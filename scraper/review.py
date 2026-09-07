"""
Ootel muudatuste ülevaatus käsurealt.

    python -m scraper.review

Siin otsustab INIMENE, kas allika ja CSV erinevus tähendab, et CSV-d tuleb
muuta. Tõendikiht (measure_facts, source_conflicts) jõuab LLM-i promptidesse
ilma selle sammuta — siin käib ainult kuvatavate CSV väärtuste muutmine.

See tööriist EI kirjuta CSV-sse ega eemalda sealt kunagi ridu. Kinnitatud
muudatused kuvatakse lõpus kopeeritava nimekirjana.
"""

import sys

from . import config, db


def _prompt(text: str) -> str:
    try:
        return input(text).strip().lower()
    except EOFError:
        return "q"


def _show(change: dict, index: int, total: int) -> None:
    risk = "⚠️  KÕRGE RISK" if change["is_high_risk"] else "madal risk"
    print()
    print("=" * 72)
    print(f"[{index}/{total}]  {change['measure_name']}")
    print(f"Väli: {change['field']}   ({risk}, kindlus: {change['confidence']})")
    print("-" * 72)
    print(f"  CSV-s praegu : {change['old_value'] or '(tühi)'}")
    print(f"  Allikas ütleb: {change['new_value'] or '(tühi)'}")
    if change["citation"]:
        print(f"  Tsitaat      : {change['citation']}")
    print(f"  Allikas      : {change['url']}")
    if change["is_high_risk"]:
        print("  -> Kontrolli algallikat enne kinnitamist.")


def main() -> int:
    with db.connect(config.STATE_DB_PATH) as conn:
        changes = db.get_unreviewed_changes(conn)
        if not changes:
            print("Ootel muudatusi ei ole.")
            return 0

        print(f"{len(changes)} ootel muudatust. Vali: [k]innita  [e]i  [j]äta vahele  [q]uit")
        approved = []

        for index, change in enumerate(changes, start=1):
            _show(change, index, len(changes))
            while True:
                answer = _prompt("  Otsus [k/e/j/q]: ")
                if answer in ("k", "e", "j", "q"):
                    break
                print("  Vasta k, e, j või q.")

            if answer == "q":
                print("\nKatkestatud. Otsustamata read jäävad ootele.")
                break
            if answer == "j":
                continue
            db.set_review_decision(conn, change["id"], approved=(answer == "k"))
            if answer == "k":
                approved.append(change)

    if approved:
        print()
        print("=" * 72)
        print("KINNITATUD — kanna need käsitsi CSV-sse:")
        print("=" * 72)
        for change in approved:
            print(f"  {change['measure_name']}  |  {change['field']}")
            print(f"      {change['old_value'] or '(tühi)'}  ->  {change['new_value']}")
        print()
        print("Master CSV: rahastusmeetmed/Rahastusmeetmed_koik.csv")
        print("NB: muuda ainult lahtri väärtust — ridu ei eemaldata.")
    else:
        print("\nKinnitatud muudatusi ei olnud.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
