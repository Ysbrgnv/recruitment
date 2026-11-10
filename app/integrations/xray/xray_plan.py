"""Bounded, explicit per-source search plans. No hidden query broadening."""
import re
from urllib.parse import urlparse
SOURCES={'linkedin':('LinkedIn','linkedin.com/in')}
DEFAULT_SOURCES=['linkedin']

def validate_sources(values):
    if not isinstance(values,list) or not values or any(not isinstance(x,str) or x not in SOURCES for x in values):raise ValueError('Выберите хотя бы одну допустимую площадку X-Ray.')
    return list(dict.fromkeys(values))

CITY_PAIRS={'алматы':('Almaty','Алматы'),'almaty':('Almaty','Алматы'),'астана':('Astana','Астана'),'astana':('Astana','Астана'),'шымкент':('Shymkent','Шымкент'),'shymkent':('Shymkent','Шымкент')}

def city_name(vacancy):
    address=vacancy.get('address') or {}
    for value in [address.get('city') if isinstance(address,dict) else None,vacancy.get('city'),vacancy.get('area')]:
        if isinstance(value,dict):value=value.get('name')
        if isinstance(value,str) and value.strip():return value.strip()
    return ''

def parse_query(line,sources):
    expected=[SOURCES[x][1] for x in validate_sources(sources)]
    line=' '.join(line.split())
    match=re.fullmatch(r'\(([^()]+)\)\s+(.+)',line) if line.startswith('(') else re.fullmatch(r'(site:\S+)\s+(.+)',line)
    if not match:raise ValueError('Укажите все выбранные сайты через OR, затем роль и город.')
    sites=[]
    for entry in re.split(r'\s+OR\s+',match.group(1)):
        if not entry.startswith('site:') or entry[5:] not in expected:raise ValueError('Невыбранная площадка.')
        if entry[5:] not in sites:sites.append(entry[5:])
    if set(sites)!=set(expected):raise ValueError('В каждой строке должны быть все выбранные площадки.')
    tail=match.group(2)
    # Permit one final city OR group only; do not allow arbitrary nested operators.
    city=re.search(r'\s+\(([^()]+)\)$',tail)
    role=tail[:city.start()] if city else tail
    if city:
        values=re.split(r'\s+OR\s+',city.group(1))
        if not 2<=len(values)<=2 or any(not re.fullmatch(r'[\w .-]+|"[\w .-]+"',v,re.UNICODE) for v in values):raise ValueError('Город: (Almaty OR Алматы).')
    if len(line)>700 or not role.strip() or role.count('"')%2 or re.search(r'\b(?:OR|AND|NOT)\b|[()]|(?:site|inurl|intitle):',role,re.I):raise ValueError('Некорректная роль X-Ray.')
    return expected,tail

def validate_plan(text,sources):
    lines=[]
    for line in text.splitlines():
        if not line.strip():continue
        sites,tail=parse_query(line,sources)
        group='('+' OR '.join('site:'+x for x in sites)+')' if len(sites)>1 else 'site:'+sites[0]
        value=group+' '+tail
        if value not in lines:lines.append(value)
    if not 1<=len(lines)<=3:raise ValueError('Допускается максимум 3 X-Ray запроса.')
    return '\n'.join(lines)

def build_plan(terms,sources,city=''):
    sites=[SOURCES[x][1] for x in validate_sources(sources)]
    group='('+' OR '.join('site:'+x for x in sites)+')' if len(sites)>1 else 'site:'+sites[0]
    variants=CITY_PAIRS.get(city.casefold(),(city,) if city else ())
    def quoted(value):return '"'+value+'"' if ' ' in value else value
    suffix=(' ('+' OR '.join(quoted(v) for v in variants)+')') if len(variants)>1 else (' '+quoted(variants[0]) if variants else '')
    tails=[]
    for line in terms.splitlines():
        line=' '.join(line.split())
        if not line:continue
        if line.startswith('(') or line.startswith('site:'):_,tail=parse_query(line,sources)
        else:tail=line
        if suffix and tail.endswith(suffix):tail=tail[:-len(suffix)].strip()
        for variant in variants:
            if tail.casefold().endswith(' '+variant.casefold()):tail=tail[:-len(variant)].strip();break
        if tail not in tails:tails.append(tail)
    if not 1<=len(tails)<=3:raise ValueError('ИИ должен вернуть от 1 до 3 вариантов, без дополнительных запросов.')
    return validate_plan('\n'.join(group+' '+tail+suffix for tail in tails),sources)

def source_for_url(url):
    p=urlparse(url);host=(p.hostname or '').lower().removeprefix('www.')
    for key,(_,site) in SOURCES.items():
        domain,_,path=site.partition('/')
        if host==domain and (not path or p.path.startswith('/'+path+'/')):return key
    return None

