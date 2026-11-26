"""Decision notifications: a webhook for every rollout that ends or freezes.

outbox.py queues one delivery per configured URL in the same transaction as
the status change; dispatcher.py sends them in the background with retries;
sink.py is a tiny receiver the local stack and CI use to see them arrive.
"""
