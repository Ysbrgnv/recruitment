"""One JSON log file per process; no cross-process RotatingFileHandler races."""
import json,logging,os,sys
from logging.handlers import RotatingFileHandler
from settings import LOG_DIR

class JsonFormatter(logging.Formatter):
    def format(self,record):
        data={'time':self.formatTime(record),'level':record.levelname,'logger':record.name,
              'pid':record.process,'process':record.processName,'message':record.getMessage()}
        for key in ('task_id','job_id','worker_id','attempt','stage'):
            if hasattr(record,key):data[key]=getattr(record,key)
        if record.exc_info:data['exception']=self.formatException(record.exc_info)
        return json.dumps(data,ensure_ascii=False)

def configure_logging():
    root=logging.getLogger()
    if getattr(root,'_recruitment_configured',False):return
    root._recruitment_configured=True;root.setLevel(logging.INFO)
    formatter=JsonFormatter()
    console=logging.StreamHandler(sys.stdout);console.setFormatter(formatter);root.addHandler(console)
    if os.getenv('LOG_TO_FILE','true').lower()=='true':
        role=os.getenv('PROCESS_ROLE','app').replace('/','_').replace('\\','_')
        handler=RotatingFileHandler(LOG_DIR/(role+'-'+str(os.getpid())+'.jsonl'),
            maxBytes=5_000_000,backupCount=3,encoding='utf-8')
        handler.setFormatter(formatter);root.addHandler(handler)

