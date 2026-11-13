"""
Tenant-Aware Hospital Knowledge & Policy Retrieval System (RAG).
Enforces tenant isolation on all operational guidelines, clinical SOPs, and formulary lookups.
"""

from typing import List, Dict, Any, Optional
import re


# Hospital Standard Operating Procedures (SOP) & Clinical Reference Knowledge Base
HOSPITAL_KNOWLEDGE_BASE: List[Dict[str, Any]] = [
    {
        "id": "SOP-BILL-001",
        "title": "Billing & Discount Policy Guidelines",
        "category": "Billing",
        "hospital_id": None,  # Global SOP applicable to all hospitals
        "content": (
            "Billing Discount Hierarchy & Ceilings:\n"
            "1. Front Desk staff have 0% discount authority. All bills generated must be at standard rate.\n"
            "2. Billing Managers have authority to apply up to a 10% discount without prior authorization.\n"
            "3. Any discount request above 10% (e.g., 20%) requires formal administrative approval.\n"
            "   The system must route such requests as pending approvals (#REQ-xxx) for Admin review in chat.\n"
            "4. Hospital Administrators hold authority for discounts up to 100% on authorized cases."
        ),
        "keywords": ["discount", "discount policy", "billing ceiling", "approval", "billing manager", "discount limit", "10%", "20%"]
    },
    {
        "id": "SOP-PHARM-002",
        "title": "Pharmacy Inventory & Batch Control SOP",
        "category": "Pharmacy",
        "hospital_id": None,  # Global SOP
        "content": (
            "Pharmacy Inventory Management Protocols:\n"
            "1. Low-stock alert threshold is triggered when quantity is less than 10 units.\n"
            "2. Expiring medicines are flagged for review if the expiry date is within the next 30 days.\n"
            "3. Pharmacists are authorized to perform inventory bulk updates up to a role cap of 500 rows.\n"
            "   Updates touching >500 rows are refused and must be escalated to Hospital Administrators.\n"
            "4. All write updates touching >50 rows require mandatory blast-radius count confirmation."
        ),
        "keywords": ["inventory", "stock", "low stock", "expiring", "expiry", "batch", "pharmacist cap", "500 rows"]
    },
    {
        "id": "SOP-SEC-003",
        "title": "Tenant Isolation & Access Control Security Policy",
        "category": "Security",
        "hospital_id": None,  # Global SOP
        "content": (
            "Tenant Isolation & Least Privilege Enforcement:\n"
            "1. Strict isolation between Hospital A and Hospital B. Staff from one hospital cannot read or modify another hospital's records.\n"
            "2. PostgreSQL Row-Level Security (RLS) is enforced on all active database sessions.\n"
            "3. Self-lockout is prohibited: Administrators cannot remove their own admin access.\n"
            "4. Privilege escalation is prohibited: Non-admin users cannot grant themselves admin roles.\n"
            "5. Ambiguous entity names (e.g. multiple staff named Ravi) require explicit clarification before changes."
        ),
        "keywords": ["tenant", "isolation", "security", "cross-tenant", "self-lockout", "escalation", "permission", "ravi"]
    },
    {
        "id": "MED-FORM-004",
        "title": "Formulary Reference: Paracetamol",
        "category": "Formulary",
        "hospital_id": None,  # Global Formulary
        "content": (
            "Paracetamol (Acetaminophen) Formulary Guidelines:\n"
            "• Indications: Mild-to-moderate pain and antipyresis (fever reduction).\n"
            "• Dosage: Adults: 500mg - 1000mg every 4-6 hours (max 4000mg/day). Pediatric: 10-15mg/kg/dose.\n"
            "• Storage: Store in a dry place below 25°C (77°F). Protect from direct light.\n"
            "• Critical Safety: Hepatotoxicity risk with overdose. Monitor liver function in chronic administration."
        ),
        "keywords": ["paracetamol", "fever", "pain", "dosage", "storage", "indication", "pediatric"]
    },
    {
        "id": "MED-FORM-005",
        "title": "Formulary Reference: Insulin & Cold Chain Storage",
        "category": "Formulary",
        "hospital_id": None,  # Global Formulary
        "content": (
            "Insulin Storage & Cold Chain Management:\n"
            "• Storage Temperature: Unopened vials/pens must be stored refrigerated at 2°C to 8°C (36°F to 46°F).\n"
            "• Never Freeze: Insulin that has been frozen must be discarded immediately.\n"
            "• In-use Insulin: Opened vials may be kept at room temperature (<25°C) for up to 28 days.\n"
            "• Inventory Alert: Immediate notification required if cold chain storage logs breach 10°C."
        ),
        "keywords": ["insulin", "cold chain", "storage", "refrigeration", "temperature", "2-8", "diabetes"]
    },
    {
        "id": "HOSP-A-SPEC-001",
        "title": "Hospital A (Apollo Specialty) Local Ward SOP",
        "category": "Local SOP",
        "hospital_id": 1,  # Scoped exclusively to Hospital A
        "content": (
            "Hospital A Local Operating Procedures:\n"
            "• Apollo Specialty Care Unit: Ward allocation priority is given to emergency surgical cases.\n"
            "• Local Contact: Emergency Duty Officer ext. 101.\n"
            "• Blood Bank reserve maintains minimum 15 units of O-negative on premises."
        ),
        "keywords": ["apollo", "ward", "hospital a", "emergency", "blood bank"]
    },
    {
        "id": "HOSP-B-SPEC-002",
        "title": "Hospital B (Care Hospital) Local Ward SOP",
        "category": "Local SOP",
        "hospital_id": 2,  # Scoped exclusively to Hospital B
        "content": (
            "Hospital B Local Operating Procedures:\n"
            "• Care Hospital General Unit: Inpatient admissions processed through Front Desk B.\n"
            "• Local Contact: Care Administrative Office ext. 202.\n"
            "• Pediatric wing visiting hours: 16:00 to 19:00 IST."
        ),
        "keywords": ["care", "hospital b", "visiting hours", "pediatric wing"]
    }
]


def is_retrieval_query(message: str) -> bool:
    """
    Detect whether a staff inquiry is asking for hospital policy, SOP, clinical guidance,
    or drug formulary information rather than executing an operational database write/read.
    """
    msg = message.lower().strip()
    
    # Policy / SOP triggers
    policy_patterns = [
        r"\b(?:policy|sop|guideline|rule|regulations?)\b",
        r"\bwhat is the (?:discount|billing|storage|return|expiry)\b",
        r"\bcan (?:a )?(?:front desk|billing manager|pharmacist|doctor)\b",
        r"\bwhat are the rules?\b",
        r"\bhow to store\b",
        r"\bstorage (?:temperature|condition|guideline)\b",
        r"\bindication|dosage|maximum dose\b",
    ]
    for pattern in policy_patterns:
        if re.search(pattern, msg):
            return True
    return False


def retrieve_hospital_guidelines(
    query: str,
    hospital_id: int,
    top_k: int = 3
) -> List[Dict[str, Any]]:
    """
    Tenant-safe retrieval of hospital guidelines, SOPs, and formulary reference.
    Guarantees:
    - Never returns documents belonging to another hospital (hospital_id isolation).
    - Returns global SOPs (hospital_id is None) and hospital-specific SOPs (hospital_id == user's hospital_id).
    """
    q_words = set(re.findall(r"\w+", query.lower()))
    scored_docs = []

    for doc in HOSPITAL_KNOWLEDGE_BASE:
        # Strict tenant boundary enforcement
        doc_hid = doc.get("hospital_id")
        if doc_hid is not None and doc_hid != hospital_id:
            # Exclude other hospitals' proprietary documents
            continue

        score = 0
        content_lower = doc["content"].lower()
        title_lower = doc["title"].lower()

        # Keyword match scoring
        for kw in doc.get("keywords", []):
            if kw in query.lower():
                score += 5

        # Word overlap scoring
        for w in q_words:
            if len(w) > 3:
                if w in title_lower:
                    score += 3
                elif w in content_lower:
                    score += 1

        if score > 0:
            scored_docs.append((score, doc))

    scored_docs.sort(key=lambda x: x[0], reverse=True)
    return [doc for _, doc in scored_docs[:top_k]]


def format_retrieval_response(docs: List[Dict[str, Any]]) -> str:
    """Format retrieved SOP and policy documents for staff display."""
    if not docs:
        return (
            "No specific hospital SOP or clinical guideline matched your query. "
            "Please consult your hospital administration manual or clinical supervisor."
        )

    formatted_sections = ["📋 **Hospital Operational & Clinical Guidelines (Retrieved SOP):**\n"]
    for i, doc in enumerate(docs, 1):
        formatted_sections.append(
            f"**[{i}] {doc['title']}** (Ref: `{doc['id']}` | Category: {doc['category']})\n"
            f"{doc['content']}\n"
        )

    return "\n".join(formatted_sections)
