"""Consulta OSV (osv.dev) para las versiones exactas de constraints.txt.

Sin dependencias: solo biblioteca estandar. Es deliberado - instalar un
escaner para auditar dependencias significa confiar en una dependencia mas.
OSV es la base que alimenta los GitHub Security Advisories y PyPI.

Sale con 1 si alguna version fijada tiene un advisory, para que el job quede
en rojo de forma visible. No bloquea nada: vive en su propio job.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import urllib.error
import urllib.request

OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/"
CONSTRAINTS = pathlib.Path(__file__).resolve().parents[1] / "constraints.txt"

# pip no es dependencia de runtime: se audita aparte al desplegar.
IGNORED = {"pip", "setuptools", "wheel"}


def pinned() -> list[tuple[str, str]]:
    """(nombre, version) de cada linea 'paquete==version' del archivo."""
    out = []
    for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].split(";", 1)[0].strip()
        match = re.fullmatch(r"([A-Za-z0-9._-]+)==([^\s]+)", line)
        if match and match.group(1).lower() not in IGNORED:
            out.append((match.group(1), match.group(2)))
    return out


def post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def detail(vuln_id: str) -> dict:
    with urllib.request.urlopen(OSV_VULN + vuln_id, timeout=30) as response:
        return json.load(response)


def main() -> int:
    packages = pinned()
    if not packages:
        print("::error::No se pudo leer ninguna version de constraints.txt")
        return 1

    queries = [{"package": {"name": n, "ecosystem": "PyPI"}, "version": v} for n, v in packages]
    try:
        results = post(OSV_BATCH, {"queries": queries})["results"]
    except (urllib.error.URLError, TimeoutError) as exc:
        # OSV caido no es un fallo del proyecto.
        print(f"::warning::No se pudo consultar OSV: {exc}")
        return 0

    flagged = [
        (name, version, [v["id"] for v in (res.get("vulns") or [])])
        for (name, version), res in zip(packages, results)
        if res.get("vulns")
    ]

    print(f"Versiones auditadas: {len(packages)}")
    if not flagged:
        print("Sin advisories conocidos.")
        return 0

    print(f"Con advisory: {len(flagged)}\n")
    for name, version, ids in flagged:
        print(f"{name}=={version}")
        for vuln_id in ids:
            try:
                data = detail(vuln_id)
            except (urllib.error.URLError, TimeoutError):
                print(f"   {vuln_id} (no se pudo ampliar)")
                continue
            aliases = ", ".join(a for a in data.get("aliases", []) if a.startswith("CVE"))
            fixed = sorted({
                event["fixed"]
                for affected in data.get("affected", [])
                for rng in affected.get("ranges", []) or []
                for event in rng.get("events", []) or []
                if "fixed" in event
            })
            print(f"   {vuln_id} [{aliases or 'sin CVE'}]")
            print(f"     {(data.get('summary') or '(sin resumen)')[:150]}")
            print(f"     corregido en: {', '.join(fixed) or 'sin version de correccion'}")
        print()

    names = ", ".join(f"{n}=={v}" for n, v, _ in flagged)
    print(f"::warning::Advisories en: {names}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
