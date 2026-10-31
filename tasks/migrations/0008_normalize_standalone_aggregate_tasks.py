"""Finish the aggregate-capacity conversion for Tasks that have no Order.

0007 merged per-unit Task rows per Order. Standalone Tasks from the removed
"Create Task" page were still one row per unit: the first unit carries the
request key and the requested quantity, later units are key-less copies. This
merges each such group into its first row (capacity = requested quantity),
repoints claim/strike/ledger history, and recomputes remaining capacity from
the real claim count. Per-unit status values become available/completed.
Re-running is safe: merged groups have no remaining siblings.
"""

from django.db import migrations

AGGREGATE_STATUSES = ("available", "completed", "unavailable")


def forwards(apps, schema_editor):
    Task = apps.get_model("tasks", "Task")
    TaskClaim = apps.get_model("tasks", "TaskClaim")
    TaskUserClaim = apps.get_model("tasks", "TaskUserClaim")
    TaskStrike = apps.get_model("tasks", "TaskStrike")
    WalletTransaction = apps.get_model("wallet", "WalletTransaction")
    alias = schema_editor.connection.alias
    tasks = Task.objects.using(alias)

    for head in tasks.filter(order__isnull=True, admin_request_key__isnull=False).order_by("id"):
        requested = max(head.admin_request_quantity, head.total_slots, 1)
        siblings = []
        if requested > 1:
            candidates = tasks.filter(
                order__isnull=True, admin_request_key__isnull=True, pk__gt=head.pk,
                title=head.title, target_link=head.target_link, reward_amount=head.reward_amount,
                unit_number__gte=2, unit_number__lte=requested,
            ).order_by("id")
            seen_units = set()
            for candidate in candidates:
                if candidate.unit_number in seen_units:
                    continue
                seen_units.add(candidate.unit_number)
                siblings.append(candidate)
        sibling_ids = [task.pk for task in siblings]
        if sibling_ids:
            TaskClaim.objects.using(alias).filter(task_id__in=sibling_ids).update(task_id=head.pk)
            TaskStrike.objects.using(alias).filter(task_id__in=sibling_ids).update(task_id=head.pk)
            WalletTransaction.objects.using(alias).filter(task_id__in=sibling_ids).update(task_id=head.pk)
            for identity in TaskUserClaim.objects.using(alias).filter(task_id__in=sibling_ids):
                if TaskUserClaim.objects.using(alias).filter(task_id=head.pk, user_id=identity.user_id).exists():
                    identity.delete()
                else:
                    TaskUserClaim.objects.using(alias).filter(pk=identity.pk).update(task_id=head.pk)
            tasks.filter(pk__in=sibling_ids).delete()
        claim_count = TaskClaim.objects.using(alias).filter(task_id=head.pk).count()
        capacity = max(requested, claim_count)
        tasks.filter(pk=head.pk).update(total_slots=capacity, unit_number=1)

    # Every standalone Task: remaining capacity derives from its real claims.
    for task in tasks.filter(order__isnull=True):
        claim_count = TaskClaim.objects.using(alias).filter(task_id=task.pk).count()
        capacity = max(task.total_slots, claim_count, 1)
        if task.status == "unavailable":
            remaining, status = 0, "unavailable"
        else:
            remaining = capacity - claim_count
            status = "completed" if remaining == 0 else "available"
        for user_id in TaskClaim.objects.using(alias).filter(task_id=task.pk).values_list("user_id", flat=True).distinct():
            TaskUserClaim.objects.using(alias).get_or_create(task_id=task.pk, user_id=user_id)
        tasks.filter(pk=task.pk).update(
            total_slots=capacity, remaining_slots=remaining, status=status,
            assigned_user_id=None, claimed_at=None, submitted_at=None,
            reviewed_at=None, reviewed_by_id=None,
        )

    leftover = tasks.exclude(status__in=AGGREGATE_STATUSES).values_list("pk", flat=True)
    if leftover:
        raise RuntimeError(f"Tasks {list(leftover)} still carry per-unit statuses; migration stopped for review.")


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0007_taskuserclaim_remove_task_uniq_task_unit_per_order_and_more"),
        ("wallet", "0004_withdrawal_upi_only_choices"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
