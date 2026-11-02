"""Checkpointed processing; collect the entire HH pool before resume evaluation."""
import json,time
from datetime import datetime
import recruitment_service as service
import operation_store as ops
from app_state import get_task,update_task,database
from tracking_service import reserve_for_ai,update_tracking
from errors import AIUnavailableError,MailUnavailableError,UserFacingError
from mail_service import send_candidate_email,send_xray_email
from xray_client import search_profiles,canonical_profile

class NeedsManual(UserFacingError):pass

def pause(task_id):
    update_task(task_id,status='waiting_manual',stage='manual_resolution',message='Исход операции неизвестен. Проверьте результат и выберите ручное действие.')
    raise NeedsManual('Исход операции неизвестен; автоматический повтор заблокирован.')

def _metrics():
    return {k:0 for k in ['search_items','scanned','already_sent','load_failed','excluded_city','excluded_experience','pool','ai_requested','structured','manual_review','emails_sent','emails_failed','duplicate_at_reservation','successful_responses']}

def _task_ops(task_id):
    with database() as con:return [ops.decode(x) for x in con.execute('SELECT * FROM operations WHERE task_id=? ORDER BY updated',(task_id,))]

def _delivery_eligible(item):
    return isinstance(item,dict) and isinstance(item.get('evaluation'),dict) and item['evaluation'].get('structured') is True

def _sync_metrics(task_id,metrics):
    operations=_task_ops(task_id)
    ai=[x for x in operations if x['kind']=='ai']
    completed=[x for x in ai if x['state']=='succeeded']
    good=[x for x in completed if _delivery_eligible(x['data'])]
    metrics.update(ai_requested=len(ai),successful_responses=len(good),structured=len(good),manual_review=sum(x['state'] in {'failed','skipped'} or (x['state']=='succeeded' and not _delivery_eligible(x['data'])) for x in ai))
    mail=[x for x in operations if x['kind']=='hh_email']
    metrics.update(emails_sent=sum(x['state']=='succeeded' and x['data'].get('status')=='Sent' for x in mail),emails_failed=sum(x['state'] in {'failed','unknown'} for x in mail))
    return good

def _migrate_saved_results(task):
    # Preserve results from the previous build, rather than re-evaluate them.
    with database() as con:
        rows=con.execute('SELECT * FROM history WHERE vacancy_id=?',(task['vacancy_id'],)).fetchall()
    for row in rows:
        data=json.loads(row['data'])
        if data.get('task_id')!=task['task_id'] or row['status']!='Succeeded' or not data.get('result'):continue
        rid=row['resume_id']
        imported = not ops.get_op(task['task_id'],'ai',rid)
        if imported:
            oid=ops.begin(task['task_id'],'ai',rid);ops.finish(oid,'succeeded',data['result'])
        if imported and not ops.get_op(task['task_id'],'hh_email',rid):
            status=data.get('email_status')
            if status in {'Sent','Displayed','Opened'}:
                oid=ops.begin(task['task_id'],'hh_email',rid);ops.finish(oid,'succeeded',{'status':status})
            elif status in {'Pending','Failed'}:
                oid=ops.begin(task['task_id'],'hh_email',rid);ops.finish(oid,'unknown',{'item':data['result'],'error':'Исход письма предыдущей версии неизвестен.'})

def _hh_mail(task,vacancy,item):
    tid=task['task_id'];rid=item['resume_id'];op=ops.get_op(tid,'hh_email',rid)
    if op and op['state'] in {'unknown','sending'}:pause(tid)
    if op and op['state']=='succeeded':return op['data'].get('status','Sent')
    oid=ops.begin(tid,'hh_email',rid,{'item':item})
    try:status=send_candidate_email(task['responsible_email'],vacancy,item,task['query'])
    except Exception as exc:
        ops.finish(oid,'unknown',{'item':item,'error':str(exc)})
        update_tracking(rid,task['vacancy_id'],'Succeeded','Structured' if item['evaluation'].get('structured') else 'Raw text',item['evaluation'].get('total'),item['evaluation'].get('level'),'OutcomeUnknown',result=item)
        pause(tid)
    ops.finish(oid,'succeeded',{'status':status})
    return status

def run_hh(task_id,progress):
    """Synchronous compatibility entry point (CLI and existing integrations)."""
    steps=run_hh_steps(task_id,progress)
    while True:
        try:next(steps)
        except StopIteration as done:return done.value

def run_hh_steps(task_id,progress,staged=False):
    task=get_task(task_id);_migrate_saved_results(task)
    if ops.unknowns(task_id):pause(task_id)
    cp=task.get('checkpoint') or {};metrics=cp.get('metrics') or _metrics()
    vacancy=cp.get('vacancy') or service.fetch_vacancy_details(task['vacancy_id'])
    target=min(service.MAX_CANDIDATE_POOL,task['requested_count']*service.POOL_MULTIPLIER+service.POOL_EXTRA)
    good=_sync_metrics(task_id,metrics)
    # Finish mail for already received answers without evaluating the resume again.
    for op in good:
        if staged:yield 'mail'
        item=op['data'];status=_hh_mail(task,vacancy,item)
        update_tracking(item['resume_id'],task['vacancy_id'],'Succeeded','Structured' if item['evaluation'].get('structured') else 'Raw text',item['evaluation'].get('total'),item['evaluation'].get('level'),status,result=item)
    if good and staged:yield 'search'
    if not cp.get('pool_complete') and len(good)<task['requested_count']:
        pool=service._search_candidate_pool(task['vacancy_id'],vacancy,service.vacancy_requirements(vacancy),task['query'],target,task['requested_count'],metrics,progress)
        # Legacy explicitly allowed retries must not disappear when rebuilding the pool.
        for op in _task_ops(task_id):
            if op['kind']=='legacy_ai' and op['state']=='retry_allowed' and op['item_key'] not in [x[0] for x in pool]:
                resume=service.fetch_resume_details(op['item_key']);pool.append((op['item_key'],resume,[]))
        cp={'vacancy':vacancy,'pool':pool,'pool_complete':True,'metrics':metrics}
        update_task(task_id,checkpoint=cp,pool_ids=[x[0] for x in pool])
    pool=cp.get('pool',[])
    progress('pool_ready',f"Пул сформирован: {len(pool)} кандидатов. Оценка после формирования пула.",service._progress_payload(metrics,target,task['requested_count']))
    yield 'ai' if staged else None  # Search worker is now released.
    for rid,resume,notes in pool:
        good=_sync_metrics(task_id,metrics)
        if len(good)>=task['requested_count']:break
        op=ops.get_op(task_id,'ai',rid)
        if op and op['state'] in {'unknown','sending'}:pause(task_id)
        if op and op['state'] in {'succeeded','skipped','failed'}:continue
        legacy=ops.get_op(task_id,'legacy_ai',rid)
        if legacy and legacy['state']=='skipped':continue
        oid=ops.reserve_and_begin_ai(task_id,rid,task['vacancy_id'],vacancy.get('name') or '',resume.get('alternate_url') or '',task['responsible_email'])
        if oid is None:
            metrics['duplicate_at_reservation']+=1;continue
        progress('ai',f"ИИ оценивает следующего кандидата; получено {len(good)} из {task['requested_count']}",service._progress_payload(metrics,target,task['requested_count']))
        try:
            item=service._build_candidate_result(vacancy,resume,len(good)+1,notes)
            item.update(resume_id=rid,url=resume.get('alternate_url') or '')
        except AIUnavailableError as exc:
            if exc.request_sent:
                ops.finish(oid,'unknown',{'resume_id':rid,'error':str(exc)})
                update_tracking(rid,task['vacancy_id'],'OutcomeUnknown','Unknown',None,'Not evaluated','Not sent',str(exc))
                pause(task_id)
            ops.record_ai_before_send_failure(oid,task_id,rid,task['vacancy_id'],str(exc))
            raise AIUnavailableError('Запрос ИИ не отправлен. Исправьте причину и нажмите «Продолжить с сохранённого этапа»: этот кандидат будет повторён без повторной оценки уже обработанных.', request_sent=False) from exc
        except Exception as exc:
            ops.finish(oid,'unknown',{'resume_id':rid,'error':str(exc)})
            update_tracking(rid,task['vacancy_id'],'OutcomeUnknown','Unknown',None,'Not evaluated','Not sent',str(exc));pause(task_id)
        # Commit every completed AI answer before the delivery gate. Invalid evaluations are preserved and never retried automatically.
        ops.finish(oid,'succeeded',item)
        update_tracking(rid,task['vacancy_id'],'Succeeded','Structured' if _delivery_eligible(item) else 'Raw text',item['evaluation'].get('total'),item['evaluation'].get('level'),'Pending' if _delivery_eligible(item) else 'Not sent',response_received_at=datetime.now(),result=item)
        if not _delivery_eligible(item):
            _sync_metrics(task_id,metrics);cp['metrics']=metrics;update_task(task_id,checkpoint=cp)
            progress('candidate_done','Оценка ИИ сохранена для ручной проверки; письмо не отправлено',service._progress_payload(metrics,target,task['requested_count']))
            yield 'ai' if staged else None
            continue
        if staged:yield 'mail'
        status=_hh_mail(task,vacancy,item)
        update_tracking(rid,task['vacancy_id'],'Succeeded','Structured',item['evaluation'].get('total'),item['evaluation'].get('level'),status,email_event_at=datetime.now(),result=item)
        _sync_metrics(task_id,metrics);cp['metrics']=metrics;update_task(task_id,checkpoint=cp)
        progress('candidate_done','Кандидат обработан',service._progress_payload(metrics,target,task['requested_count']))
        yield 'ai' if staged else None
    good=_sync_metrics(task_id,metrics)
    metrics['shortfall']=max(0,task['requested_count']-len(good))
    if metrics['shortfall']:metrics['shortfall_reason']='Сохранённый пул исчерпан или кандидаты пропущены после ручной проверки.'
    items=[x['data'] for x in good]
    manual=[x['data'] for x in _task_ops(task_id) if x['kind']=='ai' and x['state']=='succeeded' and not _delivery_eligible(x['data'])]
    return {'mode':'hh','vacancy_id':task['vacancy_id'],'requested_count':task['requested_count'],'target_pool':target,'query':task['query'],'considered':len(good),'evaluated':[x for x in items if x['evaluation'].get('structured')],'manual_review':manual,'metrics':metrics,'mail_status':f"Писем отправлено: {metrics['emails_sent']}; ошибок: {metrics['emails_failed']}"}

def run_xray(task_id,progress):
    task=get_task(task_id)
    if ops.unknowns(task_id):pause(task_id)
    cp=task.get('checkpoint') or {};vacancy=cp.get('vacancy') or service.fetch_vacancy_details(task['vacancy_id'])
    from xray_plan import source_for_url,DEFAULT_SOURCES
    selected=task.get('xray_sources',DEFAULT_SOURCES)
    counters=cp.get('filter_counts',{'history_excluded':0,'pool_duplicates':0,'pool_invalid':0})
    profiles=cp.get('profiles',[]);seen={x['url'] for x in profiles};n=task['requested_count']
    if not cp.get('pool_complete') and len(profiles)<n:
        from contextlib import closing
        with closing(search_profiles(task['query'],task_id=task_id)) as stream:
            for profile in stream:
                if len(profiles)>=n:break
                url=canonical_profile(profile.get('url'))
                if not url or source_for_url(url) not in selected:counters['pool_invalid']+=1;continue
                if url in seen:counters['pool_duplicates']+=1;continue
                seen.add(url)
                if not ops.reserve_profile(task['vacancy_id'],url,task_id):
                    counters['history_excluded']+=1
                    update_task(task_id,xray_filter_counts=dict(counters));continue
                profiles.append({'url':url})
                cp={'vacancy':vacancy,'profiles':profiles,'pool_complete':False,'filter_counts':dict(counters)};update_task(task_id,checkpoint=cp)
                progress('search',f'X-Ray: найдено {len(profiles)} из {n}',{'pool':len(profiles),'requested_count':n,'ai_requested':0})
    cp={'vacancy':vacancy,'profiles':profiles,'pool_complete':True,'filter_counts':dict(counters)};update_task(task_id,checkpoint=cp)
    op=ops.get_op(task_id,'xray_email','digest')
    status='Not sent'
    if profiles:
        if op and op['state'] in {'unknown','sending'}:pause(task_id)
        if op and op['state']=='succeeded':status=op['data'].get('status','Sent')
        else:
            oid=ops.begin(task_id,'xray_email','digest',{'profiles':profiles})
            progress('email','Отправляется X-Ray письмо со ссылками',{'pool':len(profiles),'requested_count':n,'ai_requested':0})
            try:status=send_xray_email(task['responsible_email'],vacancy,profiles,n,task['query'],oid)
            except Exception as exc:
                ops.finish(oid,'unknown',{'profiles':profiles,'error':str(exc)});ops.mark_profiles(task_id,'OutcomeUnknown');pause(task_id)
            ops.finish(oid,'succeeded',{'status':status,'profiles':profiles})
        ops.mark_profiles(task_id,status)
    shortfall=max(0,n-len(profiles))
    counts=dict((get_task(task_id) or {}).get('xray_link_counts') or {})
    counts.update(counters)
    summary=f" Прочитано ссылок: {counts.get('links_read',0)}; распознано профилей: {counts.get('profiles_recognized',0)}; не профиль/неверный URL: {counts.get('invalid_links',0)+counters['pool_invalid']}; невыбранная площадка: {counts.get('unselected_links',0)}; исключено историей: {counters['history_excluded']}; дубли: {counts.get('search_duplicates',0)+counters['pool_duplicates']}."
    return {'mode':'xray','requested_count':n,'profiles':profiles,'query':task['query'],'metrics':{'pool':len(profiles),'shortfall':shortfall,'ai_requested':0,'emails_sent':int(status=='Sent'),**counts},'message':f'Найдено {len(profiles)} из {n}. Статус письма: {status}.'+(' Недостаточно новых профилей в доступной выдаче.' if shortfall else '')+summary,'mail_status':status,'evaluated':[],'manual_review':[]}

