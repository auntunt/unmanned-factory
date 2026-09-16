"""Tiny wiring sample: one real flow, one DI edge, one dead function."""
import sys

class Gateway:
    def charge(self, amount): raise NotImplementedError

class StripeGateway(Gateway):
    def charge(self, amount): return f"stripe:{amount}"

class FakeGateway(Gateway):
    def charge(self, amount): return f"fake:{amount}"

# The concrete binding is decided here by config, NOT by the class name.
CONFIG = {"gateway": "fake"}
BINDINGS = {"stripe": StripeGateway, "fake": FakeGateway}

class PaymentService:
    def __init__(self, gateway: Gateway):
        self.gateway = gateway
    def pay(self, amount):
        return self.gateway.charge(amount)

def build_service():
    gateway = BINDINGS[CONFIG["gateway"]]()   # binding site for charge()
    return PaymentService(gateway)

def legacy_export(rows):
    """Well-named, but nothing calls it. No inbound edge from any entrypoint."""
    return "\n".join(str(r) for r in rows)

def main(argv=sys.argv[1:]):   # entrypoint
    service = build_service()
    print(service.pay(int(argv[0]) if argv else 0))

if __name__ == "__main__":
    main()
