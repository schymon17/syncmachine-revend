"""ReVend machine sync agent.

Reads the recycling machine's local MySQL database and synchronises it with
ReVend through Machine API v2: transactions, sealed bags, status, heartbeat
and logs out; product catalogue, coupons and agent updates in.

Everything that leaves the machine goes through a durable local queue first,
so nothing is lost while the machine is offline.
"""

__version__ = "3.0.0"
