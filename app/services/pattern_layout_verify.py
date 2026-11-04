"""Independent linear HPGL verifier for the exporter subset (no production parser)."""
import math
import re


def verify_export(raw, expected, expected_text=()):
    text=raw.decode('utf-8', errors='strict'); index=0; position=[0.,0.]; down=False; pen=1; lt=''; widths={}; actual=[]; labels=[]
    while index<len(text):
        command=text[index:index+2]; index+=2
        end=text.find('\x03' if command=='LB' else ';',index)
        if end<0: raise ValueError('independent verify: unterminated command')
        argument=text[index:end]; index=end+1
        if command=='LB':
            labels.append({'point':position,'pen':pen,'text':argument});continue
        if command=='IN': position=[0.,0.];down=False;pen=1;lt='';widths={}
        elif command=='SP': pen=int(argument)
        elif command=='LT': lt=argument
        elif command=='PW': widths[pen]=float(argument)
        elif command=='PA': pass
        elif command in ('PU','PD'):
            down=command=='PD'
            if not argument: continue
            tokens=argument.split(',')
            if len(tokens)%2 or any(not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?',v) for v in tokens): raise ValueError('independent verify: bad numbers')
            values=[float(v) for v in tokens]
            for x,y in zip(values[::2],values[1::2]):
                following=[x,y]
                if down: actual.append({'a':position,'b':following,'pen':pen,'lt':lt,'width':widths.get(pen)})
                position=following
        else: raise ValueError('independent verify: unsupported emitted command '+command)
    if len(actual)!=len(expected): raise ValueError('independent verify: segment count mismatch')
    for got,want in zip(actual,expected):
        if got['pen']!=want['pen'] or got['lt']!=want['lt'] or got['width']!=want['width'] or any(not math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-7) for side in ('a','b') for a,b in zip(got[side],want[side])):
            raise ValueError('independent verify: source interval transform mismatch')
    if len(labels)!=len(expected_text):raise ValueError('independent verify: label count mismatch')
    for got,want in zip(labels,expected_text):
        if got['pen']!=want['pen'] or got['text']!=want['text'] or any(not math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-7) for a,b in zip(got['point'],want['point'])):raise ValueError('independent verify: label transform mismatch')
    return {'segment_count':len(actual),'exact_source_refs_checked':True}
