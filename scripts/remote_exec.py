"""Run an explicit Python maintenance script in our existing Orca shell.

Transport contains code, never plaintext credentials. Credential provisioning
uses a one-time RSA public key and encrypted payload instead.
"""
import base64,json,os,subprocess,sys,time,uuid,zlib

ENV=os.environ['ORCA_REMOTE_ENV']
HANDLE=os.environ['ORCA_REMOTE_TERMINAL']
def call(*args):
    p=subprocess.run(['orca',*args,'--environment',ENV,'--json'],capture_output=True,text=True)
    d=json.loads(p.stdout)
    if not d.get('ok'): raise RuntimeError(d)
    return d['result']

script=open(sys.argv[1]).read()
tag=uuid.uuid4().hex
wrapped="import traceback\nprint('BEGIN "+tag+"',flush=True)\ntry:\n exec("+repr(script)+")\n print('END "+tag+" OK',flush=True)\nexcept BaseException:\n traceback.print_exc()\n print('END "+tag+" FAILED',flush=True)\n"
encoded=base64.b64encode(zlib.compress(wrapped.encode())).decode()
state=call('terminal','read','--terminal',HANDLE)
cursor=state['terminal']['latestCursor']
command="python3 -u -c \"import base64,zlib;exec(zlib.decompress(base64.b64decode('"+encoded+"')))\""
call('terminal','send','--terminal',HANDLE,'--text',command,'--enter')
started=False
for _ in range(1800):
    result=call('terminal','read','--terminal',HANDLE,'--cursor',str(cursor))['terminal']
    cursor=result['nextCursor']
    for line in result['tail']:
        if 'BEGIN '+tag in line: started=True;continue
        if line.startswith('END '+tag): sys.exit(0 if line.endswith(' OK') else 1)
        if started: print(line,flush=True)
    time.sleep(2)
raise TimeoutError('Remote script completion not observed; inspect before retrying')
