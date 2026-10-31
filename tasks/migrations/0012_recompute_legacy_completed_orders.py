"""Recompute the Orders left `completed` + `available` by the old completion rule.

The old rule completed an Order as soon as its last slot was claimed, and never
gave slots back. Under the claim-lifecycle rules a slot is held only by a
claimed, work_submitted or approved claim, and an Order is completed only when
every slot holds an approved claim. Only Orders in exactly this inconsistent
state are touched; each change is printed so it can be reviewed. On a database
without such Orders (a new production database) this does nothing.
"""

from django.db import migrations

HOLDING = ("claimed", "work_submitted", "approved")


def recompute_legacy_completed_orders(apps, schema_editor):
    Order = apps.get_model("orders", "Order")
    Task = apps.get_model("tasks", "Task")
    TaskClaim = apps.get_model("tasks", "TaskClaim")

    for order in Order.objects.filter(fulfillment_status="completed", availability_status="available").order_by("pk"):
        task = Task.objects.filter(order_id=order.pk).first()
        if task is None:
            print(f"  [tasks.0012] Order #{order.pk}: no Task found; left unchanged for manual review.")
            continue
        claims = TaskClaim.objects.filter(task_id=task.pk)
        held = claims.filter(status__in=HOLDING).count()
        approved = claims.filter(status="approved").count()
        remaining = max(0, task.total_slots - held)
        if approved >= task.total_slots:
            status = "completed"
        elif remaining == 0:
            status = "fully_claimed"
        else:
            status = "available"

        if status == "completed":
            fulfillment, availability = "completed", "fully_claimed"
        else:
            fulfillment = "in_progress" if claims.exists() else "accepted"
            if status == "fully_claimed":
                availability = "fully_claimed"
            elif remaining == task.total_slots:
                availability = "available"
            else:
                availability = "partially_claimed"

        print(
            f"  [tasks.0012] Order #{order.pk}: fulfillment {order.fulfillment_status}->{fulfillment}, "
            f"availability {order.availability_status}->{availability}; Task #{task.pk}: "
            f"status {task.status}->{status}, remaining_slots {task.remaining_slots}->{remaining} "
            f"(slots {task.total_slots}, holding claims {held}, approved {approved})"
        )
        Task.objects.filter(pk=task.pk).update(status=status, remaining_slots=remaining)
        Order.objects.filter(pk=order.pk).update(fulfillment_status=fulfillment, availability_status=availability)


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0005_order_target_type_snapshot"),
        ("tasks", "0011_claim_lifecycle_statuses"),
    ]

    operations = [
        # Not reversible from data alone; restore db.sqlite3.pre-phase-changes-20261024.bak instead.
        migrations.RunPython(recompute_legacy_completed_orders, migrations.RunPython.noop),
    ]
