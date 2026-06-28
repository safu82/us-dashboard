#!/usr/bin/env python3
"""Prediction-ledger CLI — the newsletter's call-grading loop.

The weekly loop:
  1. synthesize_newsletter.py feeds status='open' predictions to Claude, which
     grades each one in the issue prose (the trust feature).
  2. After publishing, you mark those graded here and add this issue's new calls.

Commands:
  python manage_predictions.py list [--all]
  python manage_predictions.py add "<claim>" [--resolve-after YYYY-MM-DD] [--made-on YYYY-MM-DD]
  python manage_predictions.py grade <id> <hit|miss|partial> "<note>"

The ledger (open_predictions.json) is versioned in git as the credibility record.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(BASE, 'open_predictions.json')

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass


def load():
    if not os.path.exists(LEDGER):
        return {"predictions": []}
    return json.load(open(LEDGER, encoding='utf-8'))


def save(data):
    json.dump(data, open(LEDGER, 'w', encoding='utf-8'), indent=2)


def cmd_list(data, args):
    preds = data['predictions']
    if not args.all:
        preds = [p for p in preds if p['status'] == 'open']
    if not preds:
        print('(no predictions)' if args.all else '(no open predictions)')
        return
    for p in preds:
        flag = {'hit': 'HIT', 'miss': 'MISS', 'partial': 'PARTIAL'}.get(p['status'], 'OPEN')
        print(f"#{p['id']} [{flag}] made {p['made_on']} resolve_after {p['resolve_after']}")
        print(f"    {p['claim']}")
        if p.get('note'):
            print(f"    -> {p['note']} (graded {p.get('graded_on')})")


def cmd_add(data, args):
    nid = max([p['id'] for p in data['predictions']], default=0) + 1
    data['predictions'].append({
        'id': nid,
        'made_on': args.made_on or datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        'claim': args.claim,
        'resolve_after': args.resolve_after or '',
        'status': 'open', 'grade': None, 'graded_on': None, 'note': '',
    })
    save(data)
    print(f'Added prediction #{nid}.')


def cmd_grade(data, args):
    for p in data['predictions']:
        if p['id'] == args.id:
            p['status'] = args.result
            p['grade'] = args.result
            p['graded_on'] = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            p['note'] = args.note
            save(data)
            print(f'Graded #{args.id}: {args.result.upper()} — {args.note}')
            return
    sys.exit(f'No prediction with id {args.id}')


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_list = sub.add_parser('list')
    p_list.add_argument('--all', action='store_true', help='include graded predictions')

    p_add = sub.add_parser('add')
    p_add.add_argument('claim')
    p_add.add_argument('--resolve-after', dest='resolve_after')
    p_add.add_argument('--made-on', dest='made_on')

    p_grade = sub.add_parser('grade')
    p_grade.add_argument('id', type=int)
    p_grade.add_argument('result', choices=['hit', 'miss', 'partial'])
    p_grade.add_argument('note')

    args = ap.parse_args()
    data = load()
    {'list': cmd_list, 'add': cmd_add, 'grade': cmd_grade}[args.cmd](data, args)


if __name__ == '__main__':
    main()
