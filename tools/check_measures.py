"""Validate the measure CSVs before trusting them in production.

    python -m tools.check_measures

The measure catalogue is hand-edited CSV with no schema, and several of its
failure modes are silent: a detail file whose name does not fuzzy-match its
master row leaves measure_id=None, which does not break loading — it breaks chat
threads, much later, for that one measure. This script turns those into errors
you see at edit time.

Exit code 0 = clean, 1 = at least one error. Warnings never fail the run.
"""

import argparse
import csv
import os
import sys
from collections import Counter

from backend import measures as m

# Values the filters actually branch on. Anything outside these still loads, but
# classify_requirement() silently treats it as "optional", which is rarely what
# the editor meant.
KNOWN_REQUIREMENT_VALUES = m.REQUIRED_VALUES | m.OPTIONAL_VALUES | m.NONE_VALUES
REQUIREMENT_FIELDS = ("ai", "rnd", "partner", "university")

# _applicant_categories() recognises only these tokens; a row matching none of
# them is kept for every applicant type, which is usually an editing slip.
APPLICANT_TOKENS = ("konsortsium", "mtü", "vke", "ettevõte", "ettevõtja")


class Report:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def check_measure_ids(loaded, report: Report) -> None:
    """Every measure needs a measure_id, or its chat threads cannot be created."""
    for measure in loaded:
        if not measure["measure_id"]:
            report.error(
                f"{measure['name']!r}: measure_id puudub — detailifaili 'Meetme nimetus' "
                f"ei sobitunud masteri nimega (difflib cutoff 0.6). Vestlust ei saa avada."
            )


def check_unique_names(loaded, report: Report) -> None:
    """_merge_detail_files keys by normalised name; a collision silently overwrites."""
    counts = Counter(m._norm(measure["name"]) for measure in loaded)
    for norm, n in counts.items():
        if n > 1:
            clash = [x["name"] for x in loaded if m._norm(x["name"]) == norm]
            report.error(
                f"normaliseeritud nimi {norm!r} esineb {n} korda ({', '.join(clash)}) — "
                f"detailifailide sobitamine kirjutab ühe vaikselt üle"
            )


def check_detail_files_used(loaded, report: Report) -> None:
    """A detail CSV that matched nothing is invisible work; flag it."""
    used = {measure["detail_file"] for measure in loaded if measure["detail_file"]}
    for fname in sorted(os.listdir(m.DATA_DIR)):
        if not fname.endswith(".csv") or fname == m.MASTER_FILE:
            continue
        if fname in used:
            continue
        path = os.path.join(m.DATA_DIR, fname)
        rows = list(csv.reader(open(path, encoding="utf-8")))
        has_name = any(m._cell(r, 0) == "Meetme nimetus" for r in rows)
        if not has_name:
            report.error(f"{fname}: puudub rida 'Meetme nimetus' — fail jäetakse täielikult vahele")
        else:
            report.error(f"{fname}: ei sobitunud ühegi masteri reaga")


def check_vocabulary(loaded, report: Report) -> None:
    """Requirement/status/applicant fields must use values the filters recognise."""
    for measure in loaded:
        name = measure["name"]

        for field in REQUIREMENT_FIELDS:
            value = (measure[field] or "").strip().lower()
            if value and value not in KNOWN_REQUIREMENT_VALUES and "vähemalt" not in value:
                report.warn(
                    f"{name}: {field}={measure[field]!r} pole tuntud sõnavarast — "
                    f"classify_requirement() loeb selle 'optional'-iks"
                )

        status = (measure["status"] or "").strip()
        if not status:
            report.warn(f"{name}: Staatus on tühi")

        applicant = (measure["applicant"] or "").lower()
        if applicant and not any(tok in applicant for tok in APPLICANT_TOKENS):
            report.warn(
                f"{name}: 'Sobiv taotleja'={measure['applicant']!r} ei sisalda ühtegi tuntud "
                f"märksõna — meede jääb alles KÕIGILE taotlejatüüpidele"
            )


def check_links(loaded, report: Report) -> None:
    """Link hygiene: the scraper skips non-http, and pinned RT links raise conflicts."""
    for measure in loaded:
        name = measure["name"]
        link = (measure["link"] or "").strip()
        if not link:
            report.warn(f"{name}: 'Link' on tühi — scraper jätab meetme allikad tõmbamata")
        elif not link.startswith("http"):
            report.error(f"{name}: 'Link'={link!r} ei alga http-ga — scraper jätab selle vahele")

        for extra in measure["links"]:
            extra = extra.strip()
            if not extra.startswith("http"):
                continue  # the column legitimately holds non-URL notes too
            if "riigiteataja.ee" in extra and "leiaKehtiv" not in extra:
                report.warn(
                    f"{name}: Riigi Teataja link ilma '?leiaKehtiv' parameetrita ({extra}) — "
                    f"kinnistatud link tekitab akti muutmisel 'kõrge' raskusastmega vastuolu"
                )


CHECKS = (
    check_measure_ids,
    check_unique_names,
    check_detail_files_used,
    check_vocabulary,
    check_links,
)


def run() -> Report:
    """Run every check against the on-disk CSVs and return the collected report."""
    report = Report()
    loaded = m.load_measures()
    for check in CHECKS:
        check(loaded, report)
    report.count = len(loaded)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tools.check_measures", description=__doc__)
    parser.add_argument("--strict", action="store_true", help="käsitle ka hoiatusi vigadena")
    args = parser.parse_args(argv)

    report = run()
    for w in report.warnings:
        print(f"HOIATUS  {w}")
    for e in report.errors:
        print(f"VIGA     {e}", file=sys.stderr)

    print(f"\n{report.count} meedet, {len(report.errors)} viga, {len(report.warnings)} hoiatust")
    if report.errors or (args.strict and report.warnings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
