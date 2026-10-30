"""Manual release of an old ambiguous history row with no task journal."""
import argparse,json,time
from app_state import database
import operation_store

def main():
    parser=argparse.ArgumentParser(description='Разрешить повтор старой записи только после проверки внешнего сервиса.')
    parser.add_argument('--resume-id',required=True);parser.add_argument('--vacancy-id',required=True);parser.add_argument('--reason',required=True)
    args=parser.parse_args()
    if not args.reason.strip():parser.error('Укажите основание решения.')
    with database() as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute('SELECT * FROM history WHERE resume_id=? AND vacancy_id=?',(args.resume_id,args.vacancy_id)).fetchone()
        if not row or row['status'] not in {'OutcomeUnknown','FailedAfterSend'}:parser.error('Нет старой записи с неизвестным исходом. Sending живого процесса не снимается.')
        data=json.loads(row['data']);tid=data.get('task_id')
        if tid and con.execute('SELECT 1 FROM tasks WHERE id=?',(tid,)).fetchone():parser.error('У записи есть задача. Используйте ручное решение в интерфейсе этой задачи.')
        con.execute("UPDATE history SET status='RetryAllowed',updated=? WHERE resume_id=? AND vacancy_id=?",(time.time(),args.resume_id,args.vacancy_id))
        con.execute('INSERT INTO resolution_audit(task_id,action,reason,created) VALUES(?,?,?,?)',(tid,'legacy_retry:'+args.resume_id+':'+args.vacancy_id,args.reason.strip(),time.time()))
    print('Ручное разрешение сохранено. Повтор может создать дубль, если исход был определён неверно.')

if __name__=='__main__':main()

