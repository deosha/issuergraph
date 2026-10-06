"""Command line for Assess cases.

    python -m issuergraph.assess synthetic            # build + process the labelled synthetic case
    python -m issuergraph.assess new --label L --name N [--product P --amount A]
    python -m issuergraph.assess import CASE_ID FOLDER  # copy PDFs/JSON from FOLDER (no recursion)
    python -m issuergraph.assess process CASE_ID [--force]
    python -m issuergraph.assess show CASE_ID           # statuses only; no personal values printed
    python -m issuergraph.assess unlock DOC_ID          # decrypt a password-protected PDF (prompts)

Output is limited to ids, kinds and statuses so that a terminal log of a real
case does not carry its contents.
"""
from __future__ import annotations

import argparse
import pathlib
import shutil

from ..db import connect
from . import fixtures, process


def synthetic() -> int:
    case_id = process.create_case("Synthetic salaried case (demo fixture)", fixtures.APPLICANT,
                                  "salaried", "Personal loan", 800000, synthetic=True)
    src = process.STORE / f"case_{case_id}" / "_fixture_src"
    fixtures.build(src)
    process.import_folder(case_id, src)
    syn = process.STORE / f"case_{case_id}" / process.FIXTURE_DIR_NAME
    syn.mkdir(parents=True, exist_ok=True)
    shutil.copy(src / "model_responses.json", syn / "model_responses.json")
    shutil.rmtree(src)
    process.process_case(case_id)
    return case_id


def synthetic_proprietorship() -> int:
    case_id = process.create_case("Synthetic proprietorship — business loan (demo fixture)", fixtures.OWNER,
                                  "proprietorship", "Business loan", 1500000, synthetic=True)
    src = process.STORE / f"case_{case_id}" / "_fixture_src"
    fixtures.build_proprietorship(src)
    process.import_folder(case_id, src)          # types identified from content
    shutil.rmtree(src)
    process.process_case(case_id)
    return case_id


def seed_demo_inputs(case_id: int) -> None:
    """Illustrative worksheet inputs for the synthetic case only, labelled as such."""
    with connect() as conn:
        for key, value in (("foir_max_pct", 50), ("annual_rate_pct", 11.5), ("tenure_months", 60)):
            conn.execute("INSERT INTO assess.assumption (case_id, key, value, basis) VALUES (%s,%s,%s,%s)",
                         (case_id, key, value, "Illustrative demo input for the synthetic case"))


def show(case_id: int) -> None:
    with connect() as conn:
        for d in conn.execute("SELECT id, kind, status, jsonb_array_length(status_reasons) AS n "
                              "FROM assess.document WHERE case_id=%s ORDER BY id", (case_id,)):
            print(f"  doc {d['id']:>4}  {d['kind']:<15} {d['status']:<11} reasons={d['n']}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m issuergraph.assess")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("synthetic")
    sub.add_parser("synthetic-proprietorship")
    n = sub.add_parser("new")
    n.add_argument("--label", required=True)
    n.add_argument("--name")
    n.add_argument("--product")
    n.add_argument("--amount", type=float)
    n.add_argument("--type", default="salaried", choices=("salaried", "proprietorship"))
    i = sub.add_parser("import")
    i.add_argument("case_id", type=int)
    i.add_argument("folder", type=pathlib.Path)
    p = sub.add_parser("process")
    p.add_argument("case_id", type=int)
    p.add_argument("--force", action="store_true")
    s = sub.add_parser("show")
    s.add_argument("case_id", type=int)
    u = sub.add_parser("unlock")
    u.add_argument("doc_id", type=int)
    a = ap.parse_args()
    if a.cmd == "synthetic":
        cid = synthetic()
        seed_demo_inputs(cid)
        print(f"synthetic case {cid}")
        show(cid)
    elif a.cmd == "synthetic-proprietorship":
        cid = synthetic_proprietorship()
        print(f"synthetic proprietorship case {cid}")
        show(cid)
    elif a.cmd == "new":
        print(process.create_case(a.label, a.name, a.type, a.product, a.amount))
    elif a.cmd == "import":
        ids = process.import_folder(a.case_id, a.folder)
        print(f"imported {len(ids)} document(s)")
        show(a.case_id)
    elif a.cmd == "process":
        process.process_case(a.case_id, force=a.force)
        show(a.case_id)
    elif a.cmd == "show":
        show(a.case_id)
    elif a.cmd == "unlock":
        import getpass

        process.unlock(a.doc_id, getpass.getpass("PDF password (not stored): "))
        result = process.process_document(a.doc_id, force=True)
        print(f"doc {a.doc_id}: {result['status']}")


if __name__ == "__main__":
    main()
