from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from threading import Lock


class PolicySource(ABC):
    @abstractmethod
    def policy(self, merchant_id): ...


class CarrierSource(ABC):
    @abstractmethod
    def status(self, order_id): ...


class LedgerSource(ABC):
    @abstractmethod
    def charges(self, card_ref): ...


class MerchantSource(ABC):
    @abstractmethod
    def confirmation(self, order_id): ...

    @abstractmethod
    def cancellation(self, order_id): ...

    @abstractmethod
    def refund_promise(self, order_id): ...

    @abstractmethod
    def contact(self, order_id): ...


class Submitter(ABC):
    @abstractmethod
    def submit(self, case_id, route, artefact, packet_hash): ...


class FakePolicySource(PolicySource):
    def __init__(self, data):
        self.data = data

    def policy(self, merchant_id):
        return self.data.get(merchant_id)


class FakeCarrierSource(CarrierSource):
    def __init__(self, data):
        self.data = data

    def status(self, order_id):
        return self.data.get(order_id)


class FakeLedgerSource(LedgerSource):
    def __init__(self, data):
        self.data = data

    def charges(self, card_ref):
        return list(self.data.get(card_ref, []))


class FakeMerchantSource(MerchantSource):
    def __init__(self, confirmations, cancellations=None, refunds=None, contacts=None):
        self.confirmations = confirmations
        self.cancellations = cancellations or {}
        self.refunds = refunds or {}
        self.contacts = contacts or {}

    def confirmation(self, order_id):
        return self.confirmations.get(order_id)

    def cancellation(self, order_id):
        return self.cancellations.get(order_id)

    def refund_promise(self, order_id):
        return self.refunds.get(order_id)

    def contact(self, order_id):
        value = self.contacts.get(order_id)
        if value and value.get("actor") == "merchant":
            return value
        return None


class FakeSubmitter(Submitter):
    def __init__(self):
        self.receipts = []
        self._by_case = {}
        self._lock = Lock()

    def forget(self, case_id):
        with self._lock:
            self._by_case.pop(case_id, None)
            self.receipts[:] = [row for row in self.receipts if row["case_id"] != case_id]

    def submit(self, case_id, route, artefact, packet_hash=None):
        with self._lock:
            if case_id in self._by_case:
                return self._by_case[case_id]
            # Sequence numbers remain unique after bounded receipt eviction.
            sequence = getattr(self, "_sequence", 0) + 1
            self._sequence = sequence
            receipt = f"FAKE-{sequence}"
            self.receipts.append({"receipt_id": receipt, "case_id": case_id, "route": route,
                                  "packet_hash": packet_hash})
            self._by_case[case_id] = receipt
            return receipt


@dataclass
class Sources:
    policy: PolicySource
    carrier: CarrierSource
    ledger: LedgerSource
    merchant: MerchantSource
    # Trusted adapter configuration, never populated from the submitted case.
    # order_id -> (principal_ref, card_ref, merchant_id)
    bindings: dict = field(default_factory=dict)

    def permits(self, case):
        return self.bindings.get(case.order.order_id) == (
            case.mandate.principal_ref, case.order.card_ref, case.order.merchant_id)
