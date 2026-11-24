from __future__ import annotations

RAG_CASES = [
    {
        "id": "R-HRS-01",
        "question": "What are Mumbai branch hours?",
        "relevant": ["KB-02", "KB-10"],
    },
    {
        "id": "R-HRS-02",
        "question": "शाखा कितने बजे खुलती है?",
        "relevant": ["KB-02", "KB-10"],
    },
    {
        "id": "R-VCIP-01",
        "question": "How does video KYC or V-CIP work in this demo?",
        "relevant": ["KB-03"],
    },
    {
        "id": "R-BAL-01",
        "question": "How can I check my savings balance?",
        "relevant": ["KB-01", "KB-11"],
    },
    {
        "id": "R-PII-01",
        "question": "What is my PAN number?",
        "relevant": ["KB-12"],
    },
    {
        "id": "R-IRREL-01",
        "question": "Who won the cricket world cup?",
        "relevant": [],
    },
    {
        "id": "R-CARD-01",
        "question": "Please block my debit card right now.",
        "relevant": ["KB-06", "KB-05"],
    },
    {
        "id": "R-IFSC-01",
        "question": "What is the dummy IFSC format for Sahayak Bank?",
        "relevant": ["KB-09"],
    },
]

VOICE_INTENT_HINTS = {
    "balance": ["बैलेंस", "balance", "बाकी", "शेष"],
    "other": ["लोन", "emi", "pin", "otp", "कार्ड ब्लॉक"],
}

SPECIMEN_FIELDS = {
    "name": "DEMO USER",
    "dob": "1994-01-15",
    "id_type": "DEMO_KYC",
    "id_number": "DEMO-998877",
}