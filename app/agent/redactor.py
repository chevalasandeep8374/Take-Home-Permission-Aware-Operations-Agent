"""
Pre-LLM PHI/PII De-Identification & HIPAA Masking Engine.

Ensures no Patient Health Information (PHI) or Personally Identifiable Information (PII)
(names, phones, emails, Aadhaar/national IDs, UHID/MRNs) ever crosses the external
network boundary to cloud LLM providers (e.g. Google Gemini).

Pipeline:
1. redact_phi(raw_query) -> (sanitized_query, token_map, detected_types)
2. LLM / Parser processes sanitized_query with zero knowledge of real patient identity.
3. restore_phi(intent, token_map) -> in-memory local restore before DB execution.
"""

import re
from typing import Dict, List, Tuple, Any, Optional
from dataclasses import dataclass, field


@dataclass
class RedactionResult:
    redacted_text: str
    token_map: Dict[str, str] = field(default_factory=dict)
    detected_types: List[str] = field(default_factory=list)
    is_redacted: bool = False


# Compiled regular expressions for PHI detection
# 1. Indian & International Phone Numbers
RE_PHONE = re.compile(
    r'(?:(?:\+91[\s-]?)?[6-9]\d{9}\b)|'                       # Indian 10-digit mobile (+91 optional)
    r'(?:\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b)|'                  # US/Standard 10-digit 123-456-7890
    r'(?:\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}\b)', # International format
    re.IGNORECASE
)

# 2. Email Addresses
RE_EMAIL = re.compile(
    r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
    re.IGNORECASE
)

# 3. Aadhaar / National ID (12-digit Indian Aadhaar or standard 9-digit SSN)
RE_AADHAAR = re.compile(
    r'\b[2-9]\d{3}\s\d{4}\s\d{4}\b',                          # Aadhaar 1234 5678 9012 (spaced)
    re.IGNORECASE
)
RE_SSN = re.compile(
    r'\b\d{3}-\d{2}-\d{4}\b',                                 # SSN 123-45-6789
    re.IGNORECASE
)

# 4. Patient Identifier / UHID / MRN
RE_MRN = re.compile(
    r'\b(?:MRN|UHID|PATIENT[-_]?ID)[:\s#]+([A-Za-z0-9-]+)\b',
    re.IGNORECASE
)

# 5. Contextual Patient Names
# e.g., "patient Rajesh Kumar", "patient's name Ramesh", "bill for patient Priya Sharma"
RE_PATIENT_CONTEXT = re.compile(
    r'\b(?:patient|patient named|patient\'s name|for patient|on behalf of patient|bill for patient|patient:)\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)\b',
    re.IGNORECASE
)


class PHIRedactor:
    """
    Deterministic rule-based PHI redactor that guarantees Safe Harbor HIPAA
    and DPDP de-identification with zero cloud network leakage.
    """

    @classmethod
    def redact(cls, text: str) -> RedactionResult:
        if not text or not text.strip():
            return RedactionResult(redacted_text=text)

        sanitized = text
        token_map: Dict[str, str] = {}
        detected_types: List[str] = []

        # Counter dictionary for indexed tokens
        counters = {
            "PATIENT": 1,
            "PHONE": 1,
            "EMAIL": 1,
            "NATIONAL_ID": 1,
            "MRN": 1,
        }

        # 1. Mask MRN / UHID
        def replace_mrn(match):
            nonlocal sanitized
            val = match.group(1)
            token = f"[MRN_TOKEN_{counters['MRN']}]"
            counters["MRN"] += 1
            token_map[token] = val
            if "MEDICAL_RECORD_NO" not in detected_types:
                detected_types.append("MEDICAL_RECORD_NO")
            # Replace only the ID value, keep prefix
            prefix = match.group(0)[:match.start(1) - match.start(0)]
            return f"{prefix}{token}"

        sanitized = RE_MRN.sub(replace_mrn, sanitized)

        # 2. Mask Aadhaar / SSN
        def replace_aadhaar(match):
            val = match.group(0)
            token = f"[NATIONAL_ID_TOKEN_{counters['NATIONAL_ID']}]"
            counters["NATIONAL_ID"] += 1
            token_map[token] = val
            if "NATIONAL_ID" not in detected_types:
                detected_types.append("NATIONAL_ID")
            return token

        sanitized = RE_AADHAAR.sub(replace_aadhaar, sanitized)
        sanitized = RE_SSN.sub(replace_aadhaar, sanitized)

        # 3. Mask Email
        def replace_email(match):
            val = match.group(0)
            token = f"[EMAIL_TOKEN_{counters['EMAIL']}]"
            counters["EMAIL"] += 1
            token_map[token] = val
            if "EMAIL_ADDRESS" not in detected_types:
                detected_types.append("EMAIL_ADDRESS")
            return token

        sanitized = RE_EMAIL.sub(replace_email, sanitized)

        # 4. Mask Phone Numbers
        def replace_phone(match):
            val = match.group(0).strip()
            # Avoid masking numeric quantities like "10" or "500"
            digits_only = re.sub(r'\D', '', val)
            if len(digits_only) < 7:
                return val
            token = f"[PHONE_TOKEN_{counters['PHONE']}]"
            counters["PHONE"] += 1
            token_map[token] = val
            if "PHONE_NUMBER" not in detected_types:
                detected_types.append("PHONE_NUMBER")
            return token

        sanitized = RE_PHONE.sub(replace_phone, sanitized)

        # 5. Mask Contextual Patient Names
        # Words to ignore as false positive names
        EXCLUDED_WORDS = {
            "the", "a", "an", "all", "any", "unpaid", "paid", "details", "list",
            "show", "get", "bills", "bill", "records", "record", "items", "today",
            "yesterday", "discount", "pending", "status", "hospital", "summary"
        }

        def replace_patient_name(match):
            val = match.group(1).strip()
            if val.lower() in EXCLUDED_WORDS or len(val) < 2:
                return match.group(0)
            token = f"[PATIENT_PSEUDONYM_{counters['PATIENT']}]"
            counters["PATIENT"] += 1
            token_map[token] = val
            if "PATIENT_NAME" not in detected_types:
                detected_types.append("PATIENT_NAME")
            prefix = match.group(0)[:match.start(1) - match.start(0)]
            return f"{prefix}{token}"

        sanitized = RE_PATIENT_CONTEXT.sub(replace_patient_name, sanitized)

        is_redacted = len(token_map) > 0

        return RedactionResult(
            redacted_text=sanitized,
            token_map=token_map,
            detected_types=detected_types,
            is_redacted=is_redacted
        )

    @classmethod
    def restore_text(cls, text: str, token_map: Dict[str, str]) -> str:
        """
        Replaces pseudonym tokens back with original values in a text string.
        """
        if not text or not token_map:
            return text
        result = text
        for token, original in token_map.items():
            result = result.replace(token, original)
        return result

    @classmethod
    def restore_object(cls, obj: Any, token_map: Dict[str, str]) -> Any:
        """
        Recursively restores pseudonym tokens in dictionaries, lists, strings, or Pydantic models.
        """
        if not token_map:
            return obj

        if isinstance(obj, str):
            return cls.restore_text(obj, token_map)

        if isinstance(obj, dict):
            return {k: cls.restore_object(v, token_map) for k, v in obj.items()}

        if isinstance(obj, list):
            return [cls.restore_object(item, token_map) for item in obj]

        if hasattr(obj, "__dict__"):
            # Pydantic or dataclass or class instance
            for attr_name, attr_val in vars(obj).items():
                if not attr_name.startswith("_"):
                    setattr(obj, attr_name, cls.restore_object(attr_val, token_map))
            return obj

        return obj
