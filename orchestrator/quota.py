"""Reserved seam only; no quota lookup or routing is implemented in P0."""


class QuotaProvider:
    def status(self, member_id):
        return {'memberId': member_id, 'state': 'disabled'}
