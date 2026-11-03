"""Role names shared by permission checks and Django Admin."""

REQUESTER = "ผู้ขอเบิก / ผู้รับเงิน"
SUPERVISOR_APPROVER = "หัวหน้าผู้ขอ"
ACCOUNTING_APPROVER = "ผู้อนุมัติบัญชี"
ACCOUNTANT = "เจ้าหน้าที่บัญชี"
SUPER_ADMIN = "ผู้ดูแลระบบ"
EXECUTIVE_APPROVER = "ผู้บริหาร"

ROLE_NAMES = (
    REQUESTER,
    SUPERVISOR_APPROVER,
    ACCOUNTING_APPROVER,
    ACCOUNTANT,
    SUPER_ADMIN,
    EXECUTIVE_APPROVER,
)
