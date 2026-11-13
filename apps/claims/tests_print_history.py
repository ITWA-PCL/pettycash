from django.test import TestCase
from django.template.loader import render_to_string

from apps.accounts.models import User
from .models import ClaimApproval, ClaimRequest
from .views import claim_print_final_signoffs


class PrintHistoryTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='print-owner', first_name='ผู้ขอ')
        self.manager = User.objects.create_user(username='print-manager', first_name='ผู้จัดการบัญชี')
        self.cfo = User.objects.create_user(username='print-cfo', first_name='ผู้บริหาร')
        self.claim = ClaimRequest.objects.create(requester=self.owner, accounting_manager=self.manager,
                                                cfo=self.cfo, purpose='ทดสอบพิมพ์')

    def event(self, action, before, after, actor=None):
        return self.claim.approval_records.create(action=action, status_before=before,
                                                  status_after=after, actor=actor or self.owner)

    def test_latest_submission_excludes_returned_round_and_uses_real_events(self):
        self.event('SUBMITTED', 'DRAFT', 'PENDING_SUPERVISOR')
        old = self.event('APPROVED', 'PENDING_SUPERVISOR', 'PENDING_ACCOUNTING')
        self.event('RETURNED', 'PENDING_ACCOUNTING', 'RETURNED', self.manager)
        submitted = self.event('SUBMITTED', 'RETURNED', 'PENDING_SUPERVISOR')
        head = self.event('APPROVED', 'PENDING_SUPERVISOR', 'PENDING_ACCOUNTING')
        manager = self.event('APPROVED', 'PENDING_ACCOUNTING', 'PENDING_ACCOUNTING', self.manager)
        cfo = self.event('APPROVED', 'PENDING_ACCOUNTING', 'PENDING_PAYMENT', self.cfo)
        rows = claim_print_final_signoffs(self.claim)
        self.assertEqual([row['pk'] for row in rows], [submitted.pk, head.pk, manager.pk, cfo.pk])
        self.assertNotIn(old.pk, [row['pk'] for row in rows])
        self.assertEqual(rows[-1]['label'], 'CFO ผู้อนุมัติ')

    def test_correction_replaces_accounting_approvals_but_keeps_valid_head(self):
        head = self.event('APPROVED', 'PENDING_SUPERVISOR', 'PENDING_ACCOUNTING')
        old = self.event('APPROVED', 'PENDING_ACCOUNTING', 'PENDING_PAYMENT', self.manager)
        self.event('AMOUNT_CORRECTED', 'PENDING_PAYMENT', 'PENDING_ACCOUNTING', self.manager)
        final = self.event('APPROVED', 'PENDING_ACCOUNTING', 'PENDING_PAYMENT', self.manager)
        self.assertEqual([row.get('pk') for row in claim_print_final_signoffs(self.claim)], [None, head.pk, final.pk])

    def test_settlement_keeps_initial_and_supplemental_steps_and_latest_excess(self):
        self.event('PAID', 'PENDING_PAYMENT', 'PAID', self.manager)
        self.event('RECEIVED', 'PAID', 'PENDING_CLEARANCE')
        self.event('EXCESS_SUBMITTED', 'PENDING_CLEARANCE', 'PENDING_EXCESS_APPROVAL')
        old = self.event('APPROVED', 'PENDING_EXCESS_APPROVAL', 'PENDING_PAYMENT', self.manager)
        self.event('EXCESS_SUBMITTED', 'PENDING_ACCOUNTANT_REVIEW', 'PENDING_EXCESS_APPROVAL')
        self.event('APPROVED', 'PENDING_EXCESS_APPROVAL', 'PENDING_EXCESS_APPROVAL', self.manager)
        self.event('APPROVED', 'PENDING_EXCESS_APPROVAL', 'PENDING_PAYMENT', self.cfo)
        self.event('SUPPLEMENTAL_PAID', 'PENDING_PAYMENT', 'PAID', self.manager)
        self.event('RECEIVED', 'PAID', 'COMPLETED')
        rows = claim_print_final_signoffs(self.claim)
        self.assertNotIn(old.pk, [row.get('pk') for row in rows])
        self.assertEqual([row['label'] for row in rows], ['ผู้ขอเบิก', 'ผู้อนุมัติ',
            'CFO ผู้อนุมัติ', 'ผู้จ่ายเงิน', 'ผู้รับเงิน'])

    def test_print_has_text_history_without_signature_images_or_assumed_approval(self):
        event = self.event('AUTO_RECEIVED', 'PAID', 'COMPLETED')
        event.actor = None
        event.save(update_fields=['actor'])
        self.claim.requester.signature.name = 'signatures/example.png'
        html = render_to_string('claims/claim_print.html', {'claim': self.claim,
            'final_signoffs': claim_print_final_signoffs(self.claim)})
        self.assertIn('>ลงนาม<', html)
        self.assertNotIn('ประวัติดำเนินการ', html)
        self.assertIn('ระบบอัตโนมัติ', html)
        self.assertIn('ผู้ขอไม่ได้กดยืนยัน', html)
        self.assertNotIn('<img', html)
        self.assertNotIn('Signatures', html)
        self.assertNotIn('ผู้จัดการบัญชีอนุมัติ', html)

    def test_cfo_acknowledgement_alone_does_not_add_cfo_signoff(self):
        self.event('APPROVED', 'PENDING_ACCOUNTING', 'PENDING_PAYMENT', self.manager)
        self.event('ACKNOWLEDGED', 'PENDING_PAYMENT', 'PENDING_PAYMENT', self.cfo)
        self.event('COMPLETED', 'PENDING_ACCOUNTANT_REVIEW', 'COMPLETED', self.manager)
        rows = claim_print_final_signoffs(self.claim)
        self.assertEqual([row['label'] for row in rows], ['ผู้ขอเบิก', 'ผู้อนุมัติ'])

    def test_budget_approvers_with_same_name_remain_separate_signoffs(self):
        other = User.objects.create_user(username='print-other-manager', first_name=self.manager.first_name)
        self.event('APPROVED', 'PENDING_BUDGET_APPROVAL', 'PENDING_BUDGET_APPROVAL', self.manager)
        self.event('APPROVED', 'PENDING_BUDGET_APPROVAL', 'PENDING_SUPERVISOR', other)
        rows = [row for row in claim_print_final_signoffs(self.claim) if row['label'] == 'ผู้อนุมัติ Budget']
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['actor_id'] for row in rows}, {self.manager.pk, other.pk})
