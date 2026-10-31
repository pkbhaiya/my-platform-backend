from django.db import migrations, models


def expand_tasks_to_individual_units(apps, schema_editor):
    Task = apps.get_model("tasks", "Task")
    TaskClaim = apps.get_model("tasks", "TaskClaim")
    Order = apps.get_model("orders", "Order")
    alias = schema_editor.connection.alias

    # First collect rows so creating copies does not change iteration results.
    original_tasks = list(Task.objects.using(alias).order_by("order_id", "id"))
    tasks_by_order = {}
    standalone = []
    for task in original_tasks:
        if task.order_id is None:
            standalone.append(task)
        else:
            tasks_by_order.setdefault(task.order_id, []).append(task)

    for order in Order.objects.using(alias).all().iterator():
        source_tasks = tasks_by_order.get(order.pk, [])
        if len(source_tasks) > order.quantity:
            raise RuntimeError(f"Order {order.pk} has more Task rows than ordered units; conversion was stopped without deleting records.")
        task_claims = {task.pk: list(TaskClaim.objects.using(alias).filter(task_id=task.pk).order_by("id")) for task in source_tasks}
        if any(len(claims) > 1 for claims in task_claims.values()):
            raise RuntimeError(f"Order {order.pk} has multiple claims attached to one aggregate Task. Conversion was stopped before remapping claim, strike, or ledger history.")

        if not source_tasks:
            # Historical order without materialized tasks: use the order's
            # stored pricing snapshot to create its missing individual units.
            from decimal import Decimal
            reward = (order.price_per_1000_snapshot / Decimal("1000")) * (order.user_reward_percentage_snapshot / Decimal("100"))
            for unit in range(1, order.quantity + 1):
                Task.objects.using(alias).create(
                    order_id=order.pk, unit_number=unit, total_slots=1,
                    remaining_slots=1, title="", target_link=order.target_link,
                    price_per_1000_snapshot=order.price_per_1000_snapshot,
                    user_reward_percentage_snapshot=order.user_reward_percentage_snapshot,
                    reward_amount=reward.quantize(Decimal("0.00000001")), status="available",
                )
            continue

        expanded = list(source_tasks)
        template = source_tasks[0]
        occupied_units = {task.unit_number for task in source_tasks}
        if any(unit < 1 or unit > order.quantity for unit in occupied_units):
            raise RuntimeError(f"Order {order.pk} has a Task unit number outside its ordered quantity; conversion was stopped.")
        missing_units = [unit for unit in range(1, order.quantity + 1) if unit not in occupied_units]
        for unit in missing_units:
            expanded.append(Task.objects.using(alias).create(
                order_id=order.pk, unit_number=unit, total_slots=1,
                remaining_slots=1, title=template.title, target_link=template.target_link,
                price_per_1000_snapshot=template.price_per_1000_snapshot,
                user_reward_percentage_snapshot=template.user_reward_percentage_snapshot,
                reward_amount=template.reward_amount, status="available",
            ))
        for task in source_tasks:
            claims = task_claims[task.pk]
            if claims:
                claim = claims[0]
                Task.objects.using(alias).filter(pk=task.pk).update(
                    total_slots=1, remaining_slots=0, status=claim.status,
                    assigned_user_id=claim.user_id, claimed_at=claim.claimed_at,
                    submitted_at=claim.submitted_at, reviewed_at=claim.reviewed_at,
                    reviewed_by_id=claim.reviewed_by_id,
                    submission_note=claim.submission_note, review_note=claim.review_note,
                )
            else:
                Task.objects.using(alias).filter(pk=task.pk).update(
                    total_slots=1,
                    remaining_slots=1 if task.status == "available" else 0,
                )

    for task in standalone:
        if task.total_slots <= 1:
            Task.objects.using(alias).filter(pk=task.pk).update(total_slots=1, admin_request_quantity=1, remaining_slots=0 if task.assigned_user_id else task.remaining_slots)
            continue
        requested_quantity = task.total_slots
        claims = list(TaskClaim.objects.using(alias).filter(task_id=task.pk).order_by("id"))
        if len(claims) > 1:
            raise RuntimeError(f"Standalone Task {task.pk} has multiple claims. Conversion was stopped before remapping claim, strike, or ledger history.")
        copies = [task]
        for unit in range(2, task.total_slots + 1):
            copies.append(Task.objects.using(alias).create(
                order_id=None, title=task.title, unit_number=unit, total_slots=1,
                remaining_slots=1, admin_request_key=None, target_link=task.target_link,
                price_per_1000_snapshot=task.price_per_1000_snapshot,
                user_reward_percentage_snapshot=task.user_reward_percentage_snapshot,
                reward_amount=task.reward_amount, status="available",
            ))
        for index, copied in enumerate(copies, start=1):
            if index > 1:
                Task.objects.using(alias).filter(pk=copied.pk).update(admin_request_quantity=1)
        if claims:
            claim = claims[0]
            Task.objects.using(alias).filter(pk=task.pk).update(
                total_slots=1, admin_request_quantity=requested_quantity,
                remaining_slots=0, status=claim.status, assigned_user_id=claim.user_id,
                claimed_at=claim.claimed_at, submitted_at=claim.submitted_at,
                reviewed_at=claim.reviewed_at, reviewed_by_id=claim.reviewed_by_id,
                submission_note=claim.submission_note, review_note=claim.review_note,
            )
        else:
            Task.objects.using(alias).filter(pk=task.pk).update(
                total_slots=1, admin_request_quantity=requested_quantity,
                remaining_slots=0 if task.status != "available" else 1,
            )


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0003_order_idempotency_key"),
        ("tasks", "0005_taskclaim_remove_task_uniq_user_active_claimed_task_and_more"),
        ("wallet", "0003_remove_wallettransaction_uniq_task_reward_ledger_entry_and_more"),
    ]

    operations = [
        migrations.AddField(model_name="task", name="admin_request_quantity", field=models.PositiveIntegerField(default=1, editable=False)),
        migrations.RunPython(expand_tasks_to_individual_units, migrations.RunPython.noop),
        migrations.RemoveConstraint(model_name="task", name="task_remaining_not_above_total"),
        migrations.AlterField(model_name="task", name="total_slots", field=models.PositiveIntegerField(default=1, editable=False)),
        migrations.AlterField(model_name="task", name="remaining_slots", field=models.PositiveIntegerField(default=1, editable=False)),
        migrations.RemoveConstraint(model_name="task", name="task_total_slots_positive"),
        migrations.AddConstraint(model_name="task", constraint=models.CheckConstraint(condition=models.Q(("total_slots", 1)), name="task_total_slots_positive")),
        migrations.RemoveConstraint(model_name="task", name="task_remaining_nonnegative"),
        migrations.AddConstraint(model_name="task", constraint=models.CheckConstraint(condition=models.Q(("remaining_slots__in", [0, 1])), name="task_remaining_nonnegative")),
    ]
