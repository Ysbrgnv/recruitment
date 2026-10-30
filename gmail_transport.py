"""Gmail SMTP over verified TLS. No COM, GUI, retries or credential logging."""
import os,ssl,smtplib,re
from pathlib import Path
from email.message import EmailMessage
from email.utils import formatdate,make_msgid
from errors import MailUnavailableError

def secret(name):
    value=os.getenv(name,'').strip();file=os.getenv(name+'_FILE','').strip()
    if value and file:raise MailUnavailableError('Укажите только '+name+' или '+name+'_FILE.')
    if file:
        try:value=Path(file).read_text(encoding='utf-8').strip()
        except OSError:raise MailUnavailableError('Не удалось прочитать секрет '+name+'.') from None
    return value

def address(value):
    # Exactly one ASCII mailbox, no display name, header injection or recipient lists.
    if not isinstance(value,str) or len(value)>254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}",value):
        raise MailUnavailableError('Требуется один корректный адрес электронной почты.')
    return value

def configuration():
    user=address(os.getenv('GMAIL_USER','').strip())
    password=''.join(secret('GMAIL_APP_PASSWORD').split())
    if not re.fullmatch('[A-Za-z0-9]{16}',password):
        raise MailUnavailableError('Укажите 16-символьный пароль приложения GMAIL_APP_PASSWORD, не обычный пароль Gmail.')
    try:port=int(os.getenv('GMAIL_SMTP_PORT','465'));timeout=int(os.getenv('GMAIL_TIMEOUT_SECONDS','30'))
    except ValueError:raise MailUnavailableError('Неверный порт или таймаут Gmail.') from None
    if port not in (465,587) or not 5<=timeout<=120:raise MailUnavailableError('Gmail: порт 465/587, таймаут 5..120 секунд.')
    return user,password,port,timeout

def connect():
    user,password,port,timeout=configuration();client=None
    try:
        context=ssl.create_default_context()
        if port==465:client=smtplib.SMTP_SSL('smtp.gmail.com',port,timeout=timeout,context=context)
        else:
            client=smtplib.SMTP('smtp.gmail.com',port,timeout=timeout)
            client.ehlo();client.starttls(context=context);client.ehlo()
        client.login(user,password)
        return client,user
    except Exception:
        if client:
            try:client.close()
            except Exception:pass
        raise MailUnavailableError('Не удалось подключиться к Gmail. Проверьте сеть, двухэтапную проверку и пароль приложения. Секреты не выводятся.') from None

def close(client):
    try:client.quit()
    except Exception:
        try:client.close()
        except Exception:pass

def send_html(recipient,subject,body):
    recipient=address(recipient.strip() if isinstance(recipient,str) else recipient)
    if not isinstance(subject,str) or any(c in subject for c in ('\r','\n')):raise MailUnavailableError('Некорректная тема письма.')
    # Build/serialize before connecting; fail closed on malformed headers.
    user,_,_,_=configuration()
    message=EmailMessage();message['From']=user;message['To']=recipient;message['Subject']=subject
    message['Date']=formatdate(localtime=False);message['Message-ID']=make_msgid(domain=user.split('@')[1])
    message.set_content('Письмо Recruitment Assistant. Для просмотра используйте HTML-версию.')
    message.add_alternative(body,subtype='html');message.as_bytes()
    client,_=connect()
    try:
        refused=client.send_message(message,from_addr=user,to_addrs=[recipient])
        if refused:raise MailUnavailableError('Gmail отклонил получателя. Проверьте журнал операции перед повтором.')
        # Acceptance is not proof of delivery. A failed QUIT must not repeat accepted DATA.
        return 'Sent'
    except MailUnavailableError:raise
    except Exception:
        raise MailUnavailableError('Не удалось подтвердить приём письма Gmail. Проверьте «Отправленные» перед ручным повтором; автоматический повтор запрещён.') from None
    finally:close(client)

def check_connection():
    client,_=connect()
    try:
        code,_=client.noop()
        if code!=250:raise MailUnavailableError('Gmail не подтвердил проверку соединения.')
    finally:close(client)

