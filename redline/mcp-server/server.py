"""Redline MCP server: contract scanning, redlining, clause library and version
comparison, on the same 14-trap engine as the Redline web app, SMS and email.

Run over stdio (Claude Code, Claude Desktop):   python server.py
Run over HTTP:                                  python server.py --transport streamable-http --port 8000
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

KB = json.loads((Path(__file__).parent / "kb.json").read_text(encoding="utf-8"))
BASE: int = KB["base"]
TRAPS = [{**t, "regexes": [re.compile(p, re.IGNORECASE) for p in t["patterns"]]} for t in KB["traps"]]
COUNTRIES = {c["name"].lower(): c for c in KB["countries"]}

Side = Literal["neutral", "buyer", "seller"]
Tone = Literal["soft", "balanced", "aggressive"]

# Which side each trap works against. A clause that only hurts the provider
# favours a buyer, so it is not a trap when redlining for the buyer.
HURTS = {
    "perp": {"seller"}, "noncomp": {"seller"}, "ip": {"seller"},
    "auto": {"buyer"}, "fees": {"buyer"},
}

# Firmer rewrites for an aggressive redline, written from {us}'s side.
STRONG = {
    "perp": "Any licence granted by {us} lasts only for the Term, is revocable if {them} fails to pay, and ends automatically on expiry or termination.",
    "auto": "This Agreement does not renew automatically. Any renewal requires a new written agreement signed by {us}.",
    "indem": "{them} indemnifies {us} against third-party claims arising from {them}'s breach, negligence or materials. {us} gives no indemnity beyond claims caused by its own wilful misconduct.",
    "amend": "No amendment binds {us} unless it is in writing and signed by {us}'s authorised signatory.",
    "noncomp": "{us} is free to work for anyone, including competitors of {them}. The only restriction is that {us} will not use {them}'s confidential information.",
    "ip": "{us} keeps all intellectual property and grants {them} a licence to use the paid-for deliverables for their stated purpose.",
    "ld": "{us} owes no fixed or pre-agreed damages. {them} may recover only proven direct losses, capped at the fees paid in the prior 3 months.",
    "term": "{us} may terminate for convenience on 15 days' written notice. {them} may terminate only for {us}'s uncured material breach after 30 days' written notice.",
    "liab": "{us}'s total liability is capped at the fees paid in the prior 3 months. {them}'s liability is not limited for breach of confidentiality, data protection, indemnity, gross negligence or wilful misconduct.",
    "arb": "Disputes are heard by the courts where {us} is based. Nothing in this Agreement limits {us}'s right to a jury trial or to join a class action.",
    "venue": "This Agreement is governed by the law of {us}'s home jurisdiction, and its courts hear any dispute.",
    "conf": "Confidentiality obligations are mutual and end 2 years after termination, except for trade secrets.",
    "fees": "Overdue amounts carry no extra charges or interest. {us} may withhold any amount disputed in good faith until the dispute is resolved.",
    "assign": "{them} may not assign this Agreement without {us}'s prior written consent. {us} may assign it to an affiliate or successor.",
}

PARTIES = {"buyer": ("Customer", "Provider"), "seller": ("Provider", "Customer")}

STANCE = {
    "soft": "Suggested change",
    "balanced": "Proposed change",
    "aggressive": "Required change",
}


# ---------------------------------------------------------------- engine

def _scan(text: str, side: Side = "neutral") -> dict:
    src = re.sub(r"\s+", " ", text or "")
    found = []
    for t in TRAPS:
        if side != "neutral" and side not in HURTS.get(t["id"], {"buyer", "seller"}):
            continue
        for rx in t["regexes"]:
            m = rx.search(src)
            if not m:
                continue
            dot = src.rfind(". ", 0, m.start())
            start = max(0 if dot < 0 else dot + 2, m.start() - 160)
            stop = src.find(". ", m.end())
            end = len(src) if stop < 0 else min(stop + 1, m.end() + 160)
            found.append({"id": t["id"], "label": t["label"], "weight": t["weight"], "why": t["why"], "fix": t["fix"], "quote": src[start:end].strip()})
            break
    found.sort(key=lambda t: -t["weight"])
    return {"risk": min(99, BASE + sum(t["weight"] for t in found)), "traps": found, "text": src}


def _country(name: str | None) -> dict | None:
    if not name:
        return None
    key = name.strip().lower()
    aliases = {"usa": "united states", "us": "united states", "uk": "united kingdom", "uae": "united arab emirates", "england": "united kingdom"}
    return COUNTRIES.get(aliases.get(key, key))


def _country_note(name: str | None) -> str | None:
    c = _country(name)
    if not c:
        return None
    return f"{c['name']}: {KB['family_notes'][c['family']]} Not legal advice."


def _rewrite(trap: dict, side: Side, tone: Tone) -> str:
    if tone == "aggressive" and side in PARTIES:
        us, them = PARTIES[side]
        return STRONG[trap["id"]].format(us=us, them=them)
    return trap["fix"]


def _level(risk: int) -> str:
    return "high" if risk >= 60 else "medium" if risk >= 30 else "low"


# ---------------------------------------------------------------- results

class Trap(BaseModel):
    id: str
    label: str
    weight: int
    why: str = Field(description="Why the clause is risky")
    quote: str = Field(description="The sentence in the contract that triggered it")
    safe_rewrite: str


class Analysis(BaseModel):
    risk: int = Field(description="12 (clean) to 99 (dangerous)")
    level: Literal["low", "medium", "high"]
    side: Side
    summary: str
    traps: list[Trap]
    country_note: str | None = None
    disclaimer: str = "Redline flags common contract risks. It is not legal advice."


class Change(BaseModel):
    trap: str
    stance: str
    delete: str
    insert: str
    reason: str


class Redline(BaseModel):
    risk_before: int
    risk_after: int
    side: Side
    tone: Tone
    changes: list[Change]
    redlined_text: str = Field(description="Full text with [DELETE: …] [INSERT: …] markup")
    clean_text: str = Field(description="Full text with every change accepted")


class Clause(BaseModel):
    clause: str
    side: Side
    text: str
    notes: str
    country_note: str | None = None


class Comparison(BaseModel):
    risk_v1: int
    risk_v2: int
    verdict: str
    fixed: list[str] = Field(description="Traps in v1 that v2 removed")
    introduced: list[str] = Field(description="Traps that are new in v2")
    remaining: list[str]
    sentences_removed: list[str]
    sentences_added: list[str]


# ---------------------------------------------------------------- clause library

CLAUSES = {
    "limitation of liability": {
        "aliases": ["liability", "liability cap", "lol", "limitation", "cap on liability"],
        "neutral": "Limitation of Liability. Except for fraud, gross negligence, wilful misconduct or breach of confidentiality, each party's total liability arising out of or in connection with this Agreement is limited to the fees paid or payable under it in the 12 months before the event giving rise to the claim. Neither party is liable for indirect or consequential losses, including lost profits.",
        "buyer": "Limitation of Liability. Provider's total liability is limited to the greater of (a) three times the fees paid or payable in the 12 months before the claim and (b) USD 1,000,000. No limit applies to Provider's liability for breach of confidentiality or data protection, its indemnities, gross negligence, wilful misconduct or fraud. Customer's total liability is limited to the fees paid in the 12 months before the claim.",
        "seller": "Limitation of Liability. Provider's total liability arising out of or in connection with this Agreement is limited to the fees paid in the 3 months before the event giving rise to the claim. Provider is not liable for indirect, special or consequential losses, lost profits, lost data or business interruption, however caused. Nothing limits liability for fraud or for death or personal injury caused by negligence.",
        "notes": "Courts in many countries will not enforce a cap on liability for fraud, death or personal injury, so keep those carve-outs.",
    },
    "indemnity": {
        "aliases": ["indemnification", "hold harmless"],
        "neutral": "Indemnity. Each party indemnifies the other against third-party claims to the extent caused by its own breach of this Agreement or negligence, subject to the Limitation of Liability. The indemnified party must notify the other promptly, let it control the defence, and cooperate reasonably.",
        "buyer": "Indemnity. Provider indemnifies, defends and holds harmless Customer against all third-party claims, losses and costs arising from Provider's services, deliverables, breach, negligence or infringement of any intellectual property right. This indemnity is not subject to the Limitation of Liability.",
        "seller": "Indemnity. Customer indemnifies Provider against third-party claims arising from materials or instructions Customer supplies. Provider's only indemnity is for claims that the deliverables, as delivered, infringe a third party's intellectual property, capped at the fees paid in the prior 12 months.",
        "notes": "An indemnity with no cap and no causation link is the costliest clause in most service contracts.",
    },
    "termination": {
        "aliases": ["terminate", "ending", "cancellation"],
        "neutral": "Termination. Either party may terminate this Agreement on 30 days' written notice. Either party may terminate immediately if the other commits a material breach that is not cured within 15 days of written notice. Fees for work done up to termination remain payable.",
        "buyer": "Termination. Customer may terminate this Agreement for convenience on 15 days' written notice, and immediately for any breach by Provider. On termination Provider refunds prepaid fees for work not yet performed and hands over all work in progress.",
        "seller": "Termination. Either party may terminate for material breach not cured within 30 days of notice. Customer may not terminate for convenience during the initial Term. On termination Customer pays for all work performed and any non-cancellable commitments.",
        "notes": "Check what survives termination: payment, confidentiality, liability and IP clauses usually should.",
    },
    "confidentiality": {
        "aliases": ["nda", "confidential information", "non-disclosure"],
        "neutral": "Confidentiality. Each party keeps the other's confidential information secret, uses it only to perform this Agreement and discloses it only to staff and advisers who need it and are bound by similar duties. These obligations last 3 years after termination, and for trade secrets as long as they remain secret. They do not cover information that is public, already known, independently developed or required by law to be disclosed.",
        "buyer": "Confidentiality. Provider keeps all Customer information confidential, uses it only to perform this Agreement, and returns or destroys it on request. These obligations continue for 5 years after termination and indefinitely for personal data and trade secrets.",
        "seller": "Confidentiality. Each party's obligations last 2 years after disclosure. Provider may describe Customer as a client and the work in general terms in its marketing, without disclosing confidential details.",
        "notes": "Make it mutual unless only one side is sharing anything sensitive.",
    },
    "intellectual property": {
        "aliases": ["ip", "ownership", "ip ownership", "work product"],
        "neutral": "Intellectual Property. Once paid in full, Customer owns the deliverables specified in the Statement of Work. Provider keeps its pre-existing materials, tools and know-how, and grants Customer a non-exclusive licence to use any of them included in the deliverables for as long as Customer uses those deliverables.",
        "buyer": "Intellectual Property. Customer owns all deliverables and all intellectual property created under this Agreement from creation. Provider assigns these rights to Customer and waives any moral rights to the extent permitted by law.",
        "seller": "Intellectual Property. Provider owns all intellectual property in the deliverables. On full payment Provider grants Customer a non-exclusive, non-transferable licence to use the deliverables for their stated purpose.",
        "notes": "Tie any transfer of ownership to full payment.",
    },
    "payment": {
        "aliases": ["fees", "invoicing", "payment terms", "late payment"],
        "neutral": "Payment. Provider invoices monthly in arrears. Invoices are due within 30 days of receipt. Undisputed late amounts carry simple interest at the statutory rate from 15 days after a written reminder.",
        "buyer": "Payment. Invoices are due within 45 days of receipt of a correct invoice. Customer may withhold any amount disputed in good faith. Overdue amounts carry no extra charges or interest.",
        "seller": "Payment. 50% of the fees are payable on signature and the balance on delivery. Invoices are due within 14 days. Provider may suspend work while any invoice is overdue.",
        "notes": "Some countries set mandatory late-payment interest; check the governing law.",
    },
    "governing law": {
        "aliases": ["jurisdiction", "venue", "choice of law", "law"],
        "neutral": "Governing Law and Disputes. This Agreement is governed by the law of [country]. The parties will first try in good faith to resolve any dispute within 30 days. Failing that, the courts of [city] hear the dispute.",
        "buyer": "Governing Law and Disputes. This Agreement is governed by the law of Customer's home jurisdiction, and its courts have exclusive jurisdiction.",
        "seller": "Governing Law and Disputes. This Agreement is governed by the law of Provider's home jurisdiction, and its courts have exclusive jurisdiction.",
        "notes": "Pick one country's law and one place to sue; mismatches are expensive.",
    },
    "dispute resolution": {
        "aliases": ["arbitration", "disputes", "mediation"],
        "neutral": "Dispute Resolution. Senior representatives will meet within 15 days to resolve any dispute. If that fails, either party may refer it to mediation, and after 30 days to the courts named in this Agreement. Either party may seek urgent injunctive relief at any time.",
        "buyer": "Dispute Resolution. Disputes are heard by the courts of Customer's home jurisdiction. Nothing limits Customer's right to a jury trial or to join a class action.",
        "seller": "Dispute Resolution. Disputes are finally resolved by confidential arbitration under the rules of an agreed institution, seated in Provider's home city, before a single arbitrator.",
        "notes": "Arbitration is private but can be costly for a small party.",
    },
    "auto-renewal": {
        "aliases": ["renewal", "term", "term and renewal", "auto renewal"],
        "neutral": "Term and Renewal. This Agreement lasts 12 months. It renews only by written agreement of both parties, made at least 30 days before expiry.",
        "buyer": "Term and Renewal. This Agreement lasts 12 months and does not renew automatically. Provider must remind Customer in writing 60 days before expiry.",
        "seller": "Term and Renewal. This Agreement renews for successive 12-month terms unless either party gives written notice at least 30 days before expiry. Provider will remind Customer 45 days before each renewal.",
        "notes": "Some jurisdictions require a renewal reminder for consumer and small-business contracts.",
    },
    "non-solicitation": {
        "aliases": ["non-compete", "non compete", "noncompete", "non-solicit", "restrictive covenant"],
        "neutral": "Non-Solicitation. For 6 months after termination, neither party will solicit for employment any of the other's staff it worked with directly under this Agreement. General job advertisements are not solicitation. No other restriction on either party's business applies.",
        "buyer": "Non-Solicitation. For 12 months after termination, Provider will not solicit Customer's clients or staff that it dealt with under this Agreement.",
        "seller": "Non-Solicitation. For 12 months after termination, Customer will not hire or engage any Provider staff who worked on this Agreement, directly or through a third party, without Provider's consent.",
        "notes": "Broad non-competes are void or heavily limited in many places; non-solicits are safer.",
    },
    "force majeure": {
        "aliases": ["act of god", "force majeure event"],
        "neutral": "Force Majeure. Neither party is liable for delay or failure caused by events beyond its reasonable control, such as natural disasters, war, epidemics or utility failures, if it notifies the other promptly and uses reasonable efforts to resume. If the event lasts more than 60 days, either party may terminate on written notice.",
        "buyer": "Force Majeure. Provider is excused only for events beyond its reasonable control that a business continuity plan could not have avoided. If performance is affected for more than 15 days, Customer may terminate and receive a refund of prepaid fees.",
        "seller": "Force Majeure. Provider is not liable for any delay or failure caused by events beyond its reasonable control, including supplier failures, labour disputes and cyber attacks. Payment obligations are not excused.",
        "notes": "Payment obligations are rarely excused by force majeure.",
    },
    "assignment": {
        "aliases": ["transfer", "assign", "novation"],
        "neutral": "Assignment. Neither party may assign or transfer this Agreement without the other's written consent, not to be unreasonably withheld, except to a successor in a merger or sale of substantially all its assets.",
        "buyer": "Assignment. Provider may not assign or subcontract any part of this Agreement without Customer's prior written consent. Customer may assign it to any affiliate or successor.",
        "seller": "Assignment. Provider may assign this Agreement to an affiliate or successor, and may use subcontractors while remaining responsible for their work.",
        "notes": "Change-of-control clauses matter if the other side may be acquired.",
    },
}


def _clause_key(name: str) -> str | None:
    n = name.strip().lower()
    for key, c in CLAUSES.items():
        if n == key or n in c["aliases"]:
            return key
    for key, c in CLAUSES.items():
        if key in n or any(a in n for a in c["aliases"] if len(a) > 3):
            return key
    return None


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.;])\s+", re.sub(r"\s+", " ", text or "")) if len(s.strip()) > 3]


# ---------------------------------------------------------------- tools

mcp = FastMCP("redline")


@mcp.tool()
def analyze_contract(text: str, side: Side = "neutral", country: str | None = None) -> Analysis:
    """Scan a contract for the 14 Redline traps and score its risk from 12 (clean) to 99.

    Args:
        text: The full contract text.
        side: Whose interests to protect. "buyer" is the paying customer, "seller" the provider.
              Clauses that only favour that side are not counted as traps.
        country: Optional country whose law governs the contract, for a short legal-system note.
    """
    r = _scan(text, side)
    n = len(r["traps"])
    summary = (f"{n} trap{'s' if n != 1 else ''}: " + ", ".join(t["label"] for t in r["traps"]) + ".") if n else "No known traps found."
    return Analysis(
        risk=r["risk"], level=_level(r["risk"]), side=side, summary=summary,
        traps=[Trap(id=t["id"], label=t["label"], weight=t["weight"], why=t["why"], quote=t["quote"], safe_rewrite=t["fix"]) for t in r["traps"]],
        country_note=_country_note(country),
    )


@mcp.tool()
def redline_contract(text: str, side: Side = "neutral", tone: Tone = "balanced") -> Redline:
    """Redline a contract: strike each risky sentence and insert safer wording.

    Args:
        text: The full contract text.
        side: "buyer" (paying customer), "seller" (provider) or "neutral" (fair to both).
        tone: "soft" suggests, "balanced" proposes mutual wording, "aggressive" demands
              one-sided wording that favours the chosen side.
    """
    r = _scan(text, side)
    redlined, clean, changes = r["text"], r["text"], []
    for t in r["traps"]:
        new = _rewrite(t, side, tone)
        changes.append(Change(trap=t["label"], stance=STANCE[tone], delete=t["quote"], insert=new, reason=t["why"]))
        redlined = redlined.replace(t["quote"], f"[DELETE: {t['quote']}] [INSERT: {new}]", 1)
        clean = clean.replace(t["quote"], new, 1)
    return Redline(risk_before=r["risk"], risk_after=_scan(clean, side)["risk"], side=side, tone=tone, changes=changes, redlined_text=redlined, clean_text=clean)


@mcp.tool()
def get_clause(clause_type: str, side: Side = "neutral", country: str | None = None) -> Clause:
    """Get a ready-to-use clause from the Redline library.

    Args:
        clause_type: e.g. "limitation of liability", "indemnity", "termination", "confidentiality",
                     "intellectual property", "payment", "governing law", "dispute resolution",
                     "auto-renewal", "non-solicitation", "force majeure", "assignment".
        side: "buyer", "seller" or "neutral" wording.
        country: Optional governing-law country for a legal-system note.
    """
    key = _clause_key(clause_type)
    if not key:
        raise ValueError(f"Unknown clause type {clause_type!r}. Available: {', '.join(CLAUSES)}.")
    c = CLAUSES[key]
    return Clause(clause=key, side=side, text=c[side], notes=c["notes"], country_note=_country_note(country))


@mcp.tool()
def compare_versions(v1: str, v2: str, side: Side = "neutral") -> Comparison:
    """Compare two versions of a contract: which traps were fixed, which are new, and how the risk moved.

    Args:
        v1: The earlier version.
        v2: The later version.
        side: Whose interests to judge the change by.
    """
    a, b = _scan(v1, side), _scan(v2, side)
    ia, ib = {t["label"] for t in a["traps"]}, {t["label"] for t in b["traps"]}
    sa, sb = _sentences(v1), _sentences(v2)
    sm = difflib.SequenceMatcher(a=sa, b=sb, autojunk=False)
    removed, added = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op in ("delete", "replace"):
            removed += sa[i1:i2]
        if op in ("insert", "replace"):
            added += sb[j1:j2]
    delta = b["risk"] - a["risk"]
    verdict = ("v2 is safer" if delta < 0 else "v2 is riskier" if delta > 0 else "Risk is unchanged") + f": {a['risk']} → {b['risk']} ({delta:+d})."
    return Comparison(
        risk_v1=a["risk"], risk_v2=b["risk"], verdict=verdict,
        fixed=sorted(ia - ib), introduced=sorted(ib - ia), remaining=sorted(ia & ib),
        sentences_removed=removed[:20], sentences_added=added[:20],
    )


@mcp.tool()
def country_note(country: str) -> str:
    """A short note on how contracts work under a country's legal system (195 countries)."""
    note = _country_note(country)
    if not note:
        raise ValueError(f"Unknown country {country!r}. Use the English short name, e.g. 'Germany' or 'United States'.")
    return note


@mcp.tool()
def list_traps() -> list[dict]:
    """The 14 traps Redline checks for, with their weights and safe rewrites."""
    return [{"id": t["id"], "label": t["label"], "weight": t["weight"], "why": t["why"], "safe_rewrite": t["fix"]} for t in TRAPS]


def main() -> None:
    parser = argparse.ArgumentParser(description="Redline MCP server")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.transport != "stdio":
        mcp.settings.host, mcp.settings.port = args.host, args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
