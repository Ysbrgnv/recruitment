"""Independent HH search, AI and mail pools. One active slice per task."""
import logging,threading,time
from concurrent.futures import ThreadPoolExecutor
from types import GeneratorType
from app.core.settings import HH_BACKGROUND_WORKERS,HH_AI_WORKERS,HH_MAIL_WORKERS
logger=logging.getLogger(__name__)
class HHScheduler:
    def __init__(self,workers=HH_BACKGROUND_WORKERS,ai_workers=HH_AI_WORKERS,mail_workers=HH_MAIL_WORKERS):
        self.executor=ThreadPoolExecutor(max_workers=workers,thread_name_prefix='hh-search')
        self.ai_executor=ThreadPoolExecutor(max_workers=ai_workers,thread_name_prefix='hh-ai')
        self.mail_executor=ThreadPoolExecutor(max_workers=mail_workers,thread_name_prefix='hh-mail')
        self.pools={'search':self.executor,'ai':self.ai_executor,'mail':self.mail_executor}
        self.lock=threading.RLock();self.active={}
    def submit(self,runner,task_id,prepare=False):
        with self.lock:
            if task_id in self.active:return False
            self.active[task_id]=True
            try:self._queue(runner,task_id,prepare,None,'ai' if prepare else 'search')
            except Exception:
                self._failure(task_id);self.active.pop(task_id,None);raise
        return True
    def _queue(self,runner,task_id,prepare,steps,stage='search'):
        future=self.pools[stage].submit(runner,task_id,prepare,True,steps)
        future.add_done_callback(lambda done:self._done(done,runner,task_id))
    def _failure(self,task_id):
        logger.exception('HH scheduler failed for task %s',task_id)
        try:
            from app_state import get_task,update_task
            import operation_store as ops
            if not get_task(task_id):return
            ops.mark_inflight_unknown(task_id)
            if ops.unknowns(task_id):
                update_task(task_id,status='waiting_manual',stage='manual_resolution',message='Сбой планировщика. Исход операции неизвестен; требуется ручная проверка.')
            else:update_task(task_id,status='failed',stage='failed',message='Сбой планировщика HH',error='Не удалось продолжить этап. Сохранённые результаты доступны; возобновите задачу.',finished=time.time())
        except Exception:logger.exception('Cannot persist scheduler failure for %s',task_id)
    def _done(self,future,runner,task_id):
        try:
            result=future.result()
            if isinstance(result,tuple) and len(result)==2 and isinstance(result[0],GeneratorType):steps,stage=result
            elif isinstance(result,GeneratorType):steps,stage=result,'search'
            else:steps=None
            if steps is not None:
                with self.lock:self._queue(runner,task_id,False,steps,stage or 'search')
                return
        except Exception:self._failure(task_id)
        with self.lock:self.active.pop(task_id,None)
    def shutdown(self):
        for pool in self.pools.values():pool.shutdown()
HH_SCHEDULER=HHScheduler()

