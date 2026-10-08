"""Chat with SafeAgent in your terminal (uses the real Groq model).

Run from the backend folder:   python try_agent.py

Commands while chatting:
  /who CUS-002   switch which customer you are (CUS-001, CUS-002, CUS-003)
  /audit         show the audit log
  /approve 3     approve pending request #3
  /reject 3      reject pending request #3
  /reset         reset the demo data
  /quit          exit
"""
from app import db
from app.agent import ApprovalError, build_graph, handle_message, resolve_approval

conn = db.get_conn()
db.seed(conn)
graph = build_graph(conn)
customer = "CUS-001"

print(__doc__)
print(f"You are {customer}. Try: 'Where is my order ORD-1001?' or 'Refund $200 for ORD-1002, it was damaged'\n")

while True:
    text = input(f"[{customer}] > ").strip()
    if not text:
        continue
    if text == "/quit":
        break
    if text == "/reset":
        db.seed(conn)
        print("Demo data reset.\n")
    elif text.startswith("/who"):
        customer = text.split()[1]
        print(f"Now chatting as {customer}\n")
    elif text == "/audit":
        for row in reversed(db.list_audit(conn)):
            print(f"#{row['id']:<3} {row['customer_id']} {row['tool']:<22} risk={row['risk']:<8} "
                  f"status={row['status']:<18} args={row['args']}")
        print()
    elif text.startswith(("/approve", "/reject")):
        cmd, audit_id = text.split()
        try:
            print(resolve_approval(conn, int(audit_id), approve=(cmd == "/approve")), "\n")
        except ApprovalError as exc:
            print("Error:", exc, "\n")
    else:
        out = handle_message(graph, customer, text)
        print(f"  bot: {out['reply']}")
        print(f"  [outcome={out['outcome']} risk={out['risk']} decision={out['decision']} audit=#{out['audit_id']}]\n")
