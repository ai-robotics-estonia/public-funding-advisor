"""Manage tester accounts for the pilot deployment.

    python -m tools.testers add "Mari Maasikas"
    python -m tools.testers list
    python -m tools.testers disable <tester_id>
    python -m tools.testers enable  <tester_id>

The login code is printed exactly once, at creation. Only its keyed hash is
stored, so a lost code cannot be recovered — disable the tester and add a new
one instead.

SESSION_SECRET must be the same value the server runs with: the stored hash is
keyed with it, so codes minted under a different secret will never match.
"""

import argparse
import sys

from dotenv import load_dotenv

load_dotenv()

from backend import auth, db  # noqa: E402


def cmd_add(args) -> int:
    db.init_db()
    code = auth.generate_code()
    tester_id = db.add_tester(args.name, auth.hash_code(code))
    print(f"Lisatud: {args.name}")
    print(f"  tester_id : {tester_id}")
    print(f"  kood      : {code}")
    print()
    print("Kood kuvatakse ainult praegu — salvesta see enne akna sulgemist.")
    return 0


def cmd_list(args) -> int:
    db.init_db()
    rows = db.list_testers()
    if not rows:
        print("Ühtegi testijat pole veel lisatud.")
        return 0
    print(f"{'tester_id':<14} {'aktiivne':<9} {'viimati nähtud':<28} nimi")
    for r in rows:
        print(f"{r['tester_id']:<14} {'jah' if r['active'] else 'ei':<9} "
              f"{(r['last_seen_at'] or '-'):<28} {r['name']}")
    return 0


def _set_active(tester_id: str, active: bool) -> int:
    db.init_db()
    if not db.set_tester_active(tester_id, active):
        print(f"Tundmatu tester_id: {tester_id}", file=sys.stderr)
        return 1
    print(f"{tester_id}: {'lubatud' if active else 'keelatud'}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tools.testers", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="lisa uus testija ja väljasta kood")
    p_add.add_argument("name")
    p_add.set_defaults(func=cmd_add)

    sub.add_parser("list", help="näita kõiki testijaid").set_defaults(func=cmd_list)

    p_dis = sub.add_parser("disable", help="keela testija kood")
    p_dis.add_argument("tester_id")
    p_dis.set_defaults(func=lambda a: _set_active(a.tester_id, False))

    p_en = sub.add_parser("enable", help="luba testija kood uuesti")
    p_en.add_argument("tester_id")
    p_en.set_defaults(func=lambda a: _set_active(a.tester_id, True))

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
