import os
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

procs = {}


def reader(name, p):
    for line in p.stdout:
        sys.stdout.write(f'[{name}] {line}')
        sys.stdout.flush()


def start(name, args):
    p = subprocess.Popen(
        [PY, '-u'] + args,
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    procs[name] = p
    threading.Thread(target=reader, args=(name, p), daemon=True).start()


def send(name, line):
    procs[name].stdin.write(line + '\n')
    procs[name].stdin.flush()


def step(label, fn=None, wait=1.0):
    print(f'\n>>> {label}')
    if fn:
        fn()
    time.sleep(wait)


start('admin', ['administrator.py'])
time.sleep(1)
start('NASA', ['agencja.py', 'NASA'])
start('ESA', ['agencja.py', 'ESA'])
time.sleep(1)
start('C1', ['przewoznik.py', 'C1', 'osoby', 'ladunek'])
start('C2', ['przewoznik.py', 'C2', 'ladunek', 'satelita'])
time.sleep(1.5)

step('NASA osoby (only C1)',          lambda: send('NASA', 'osoby'))
step('NASA satelita (only C2)',       lambda: send('NASA', 'satelita'))
step('ESA ladunek x4 (round-robin)',  lambda: [send('ESA', 'ladunek') for _ in range(4)], wait=1.5)
step('admin -> agencies',             lambda: send('admin', 'agencies hi-agencies'))
step('admin -> carriers',             lambda: send('admin', 'carriers hi-carriers'))
step('admin -> all',                  lambda: send('admin', 'all hi-everyone'))

step('shutdown')
for n in ('NASA', 'ESA', 'admin'):
    send(n, 'quit')
time.sleep(0.5)
for p in procs.values():
    p.terminate()
