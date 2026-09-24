"""Pure performance math: size scaling intersected with machine-type ceilings.

These functions contain no I/O and no Google-specific lookups beyond the typed
models they are handed, which makes them cheap to property-test.  The core rule
is the documented one::

    achievable = MIN(instance_limit, disk_scaling(size), disk_type_cap)

so the "intersection point" the optimizer cares about is the disk size at which
``disk_scaling(size)`` reaches ``instance_limit`` -- beyond it, buying more
capacity no longer buys performance.
"""

from __future__ import annotations

from decimal import Decimal

from gcp_opt.errors import InfeasibleTargetError
from gcp_opt.models import (
    BindingConstraints,
    DiskPerformanceModel,
    LimitSource,
    MachineTypeDiskLimit,
    PerformanceEnvelope,
)
from gcp_opt.units import DecimalLike, to_decimal

#: Lower value wins; on a tie the more "external" constraint is reported as binding
#: so that hitting the machine ceiling at the exact elbow is attributed to the VM.
_SOURCE_PRIORITY: dict[LimitSource, int] = {
    LimitSource.INSTANCE: 0,
    LimitSource.DISK_TYPE_CAP: 1,
    LimitSource.DISK_MODEL: 2,
}


def _ceiling(
    instance_cap: Decimal | None,
    model_value: Decimal,
    type_cap: Decimal | None,
) -> tuple[Decimal, LimitSource]:
    candidates: list[tuple[Decimal, LimitSource]] = [(model_value, LimitSource.DISK_MODEL)]
    if type_cap is not None:
        candidates.append((type_cap, LimitSource.DISK_TYPE_CAP))
    if instance_cap is not None:
        candidates.append((instance_cap, LimitSource.INSTANCE))
    value, source = min(candidates, key=lambda item: (item[0], _SOURCE_PRIORITY[item[1]]))
    return value, source


def achievable_performance(
    model: DiskPerformanceModel,
    size_gib: DecimalLike,
    *,
    machine_limit: MachineTypeDiskLimit | None = None,
    provisioned_iops: DecimalLike | None = None,
) -> PerformanceEnvelope:
    """Compute achievable read/write IOPS and throughput for one configuration.

    Args:
        model: scaling rules for the disk type.
        size_gib: combined size of all volumes of this type on the instance.
        machine_limit: per-machine-type ceilings, or ``None`` if unknown.
        provisioned_iops: required for provisioned-IOPS disks (``pd-extreme``).

    Returns:
        The envelope plus the constraint that produced each ceiling.

    Raises:
        ValueError: if a provisioned-IOPS disk is used without ``provisioned_iops``.
    """
    size = to_decimal(size_gib)
    if size < 0:
        raise ValueError(f"size_gib must be non-negative, got {size}")

    iops_instance_read = machine_limit.max_read_iops if machine_limit else None
    iops_instance_write = machine_limit.max_write_iops if machine_limit else None
    mibps_instance_read = machine_limit.max_read_mibps if machine_limit else None
    mibps_instance_write = machine_limit.max_write_mibps if machine_limit else None

    if model.provisioned_iops:
        if provisioned_iops is None:
            raise ValueError(
                f"{model.disk_kind} performance is provisioned, not size-scaled; "
                "pass provisioned_iops"
            )
        provisioned = to_decimal(provisioned_iops)
        per_iop = model.throughput_mibps_per_provisioned_iop or Decimal(0)
        read_iops = write_iops = provisioned
        read_mibps = write_mibps = provisioned * per_iop
    else:
        read_iops = model.scaled_iops(size, write=False)
        write_iops = model.scaled_iops(size, write=True)
        read_mibps = model.scaled_mibps(size, write=False)
        write_mibps = model.scaled_mibps(size, write=True)

    ri, ri_src = _ceiling(iops_instance_read, read_iops, model.type_cap_iops(write=False))
    wi, wi_src = _ceiling(iops_instance_write, write_iops, model.type_cap_iops(write=True))
    rm, rm_src = _ceiling(mibps_instance_read, read_mibps, model.type_cap_mibps(write=False))
    wm, wm_src = _ceiling(mibps_instance_write, write_mibps, model.type_cap_mibps(write=True))

    return PerformanceEnvelope(
        read_iops=ri,
        write_iops=wi,
        read_mibps=rm,
        write_mibps=wm,
        binding=BindingConstraints(
            read_iops=ri_src, write_iops=wi_src, read_mibps=rm_src, write_mibps=wm_src
        ),
    )


def _size_to_reach(
    target: Decimal,
    *,
    per_gib: Decimal,
    base: Decimal,
    instance_cap: Decimal | None,
    type_cap: Decimal | None,
    target_name: str,
) -> Decimal:
    """Return the smallest size whose scaled value reaches ``target``.

    Raises:
        InfeasibleTargetError: if a cap makes ``target`` unreachable at any size.
    """
    if instance_cap is not None and instance_cap < target:
        raise InfeasibleTargetError(
            f"target {target_name}={target} exceeds the machine-type ceiling {instance_cap}",
            target=target_name,
            limit_kind="instance_machine_type",
        )
    if type_cap is not None and type_cap < target:
        raise InfeasibleTargetError(
            f"target {target_name}={target} exceeds the disk-type cap {type_cap}",
            target=target_name,
            limit_kind="disk_type_cap",
        )
    if per_gib == 0:
        if base >= target:
            return Decimal(0)
        raise InfeasibleTargetError(
            f"target {target_name}={target} is unreachable: disk does not scale with size",
            target=target_name,
            limit_kind="disk_model",
        )
    needed = (target - base) / per_gib
    return max(needed, Decimal(0))


def required_size_gib(
    model: DiskPerformanceModel,
    *,
    read_iops: DecimalLike | None = None,
    write_iops: DecimalLike | None = None,
    read_mibps: DecimalLike | None = None,
    write_mibps: DecimalLike | None = None,
    machine_limit: MachineTypeDiskLimit | None = None,
) -> Decimal:
    """Return the smallest size (GiB) meeting every supplied performance target.

    This is the analytic inverse of :func:`achievable_performance` and is what
    makes "cheapest size that reaches X" a closed-form answer for a fixed
    machine type and disk kind.

    Raises:
        ValueError: for provisioned-IOPS disks, whose IOPS are not size-driven.
        InfeasibleTargetError: if any target exceeds a hard cap.
    """
    if model.provisioned_iops:
        raise ValueError(
            f"{model.disk_kind} is provisioned by IOPS; size does not determine performance"
        )

    ceilings = {
        "read_iops": max(model.iops_per_gib_read, Decimal(0)),
        "write_iops": max(model.iops_per_gib_write, Decimal(0)),
        "read_mibps": max(model.throughput_mibps_per_gib_read, Decimal(0)),
        "write_mibps": max(model.throughput_mibps_per_gib_write, Decimal(0)),
    }
    bases = {
        "read_iops": model.iops_base_read,
        "write_iops": model.iops_base_write,
        "read_mibps": model.throughput_mibps_base_read,
        "write_mibps": model.throughput_mibps_base_write,
    }
    instance_caps: dict[str, Decimal | None] = {
        "read_iops": machine_limit.max_read_iops if machine_limit else None,
        "write_iops": machine_limit.max_write_iops if machine_limit else None,
        "read_mibps": machine_limit.max_read_mibps if machine_limit else None,
        "write_mibps": machine_limit.max_write_mibps if machine_limit else None,
    }
    type_caps: dict[str, Decimal | None] = {
        "read_iops": model.max_read_iops,
        "write_iops": model.max_write_iops,
        "read_mibps": model.max_read_mibps,
        "write_mibps": model.max_write_mibps,
    }

    targets = {
        "read_iops": read_iops,
        "write_iops": write_iops,
        "read_mibps": read_mibps,
        "write_mibps": write_mibps,
    }

    smallest = Decimal(0)
    for name, raw_target in targets.items():
        if raw_target is None:
            continue
        smallest = max(
            smallest,
            _size_to_reach(
                to_decimal(raw_target),
                per_gib=ceilings[name],
                base=bases[name],
                instance_cap=instance_caps[name],
                type_cap=type_caps[name],
                target_name=name,
            ),
        )

    if model.min_size_gib is not None:
        smallest = max(smallest, model.min_size_gib)
    return smallest


def required_provisioned_iops(
    model: DiskPerformanceModel,
    *,
    read_iops: DecimalLike | None = None,
    write_iops: DecimalLike | None = None,
    read_mibps: DecimalLike | None = None,
    write_mibps: DecimalLike | None = None,
    machine_limit: MachineTypeDiskLimit | None = None,
) -> Decimal:
    """Minimum provisioned IOPS meeting every supplied target.

    For provisioned-performance disks such as ``pd-extreme`` the provisioned level
    drives both IOPS and throughput (Extreme PD: 256 KiB/s per provisioned IOPS),
    so a throughput target ``t`` MiB/s requires ``t / 0.25`` IOPS.

    Raises:
        ValueError: for disks that are not provisioned by IOPS.
        InfeasibleTargetError: if any target exceeds a machine or disk-type cap.
    """
    if not model.provisioned_iops:
        raise ValueError(
            f"{model.disk_kind} is size-scaled, not provisioned; use required_size_gib()"
        )

    per_iop = model.throughput_mibps_per_provisioned_iop
    required = Decimal(0)

    def demand(
        target: DecimalLike,
        *,
        instance_cap: Decimal | None,
        type_cap: Decimal | None,
        target_name: str,
    ) -> None:
        nonlocal required
        value = to_decimal(target)
        if instance_cap is not None and value > instance_cap:
            raise InfeasibleTargetError(
                f"target {target_name}={value} exceeds the machine-type ceiling {instance_cap}",
                target=target_name,
                limit_kind="instance_machine_type",
            )
        if type_cap is not None and value > type_cap:
            raise InfeasibleTargetError(
                f"target {target_name}={value} exceeds the disk-type cap {type_cap}",
                target=target_name,
                limit_kind="disk_type_cap",
            )
        required = max(required, value)

    for name, target, instance_cap, type_cap in (
        (
            "read_iops",
            read_iops,
            machine_limit.max_read_iops if machine_limit else None,
            model.max_read_iops,
        ),
        (
            "write_iops",
            write_iops,
            machine_limit.max_write_iops if machine_limit else None,
            model.max_write_iops,
        ),
    ):
        if target is not None:
            demand(target, instance_cap=instance_cap, type_cap=type_cap, target_name=name)

    throughput_targets = [
        (
            "read_mibps",
            read_mibps,
            machine_limit.max_read_mibps if machine_limit else None,
            model.max_read_mibps,
            machine_limit.max_read_iops if machine_limit else None,
            model.max_read_iops,
        ),
        (
            "write_mibps",
            write_mibps,
            machine_limit.max_write_mibps if machine_limit else None,
            model.max_write_mibps,
            machine_limit.max_write_iops if machine_limit else None,
            model.max_write_iops,
        ),
    ]
    for name, target, instance_mibps, type_mibps, instance_iops, type_iops in throughput_targets:
        if target is None:
            continue
        demand(target, instance_cap=instance_mibps, type_cap=type_mibps, target_name=name)
        if per_iop is None or per_iop <= 0:
            raise InfeasibleTargetError(
                f"{model.disk_kind} has no throughput-per-IOPS model",
                target=name,
                limit_kind="disk_model",
            )
        # The IOPS level needed to sustain the throughput must itself fit the caps.
        demand(
            to_decimal(target) / per_iop,
            instance_cap=instance_iops,
            type_cap=type_iops,
            target_name=f"{name}_as_iops",
        )

    if model.provisioned_iops_max is not None and required > model.provisioned_iops_max:
        raise InfeasibleTargetError(
            f"targets need {required} IOPS but the disk-type maximum is "
            f"{model.provisioned_iops_max}",
            target="provisioned_iops",
            limit_kind="disk_type_cap",
        )
    if model.provisioned_iops_min is not None:
        required = max(required, model.provisioned_iops_min)
    return required


def knee_size_gib(
    model: DiskPerformanceModel,
    machine_limit: MachineTypeDiskLimit,
    *,
    write: bool = False,
    throughput: bool = False,
) -> Decimal | None:
    """Size at which the machine-type ceiling starts to bind for one direction.

    Beyond this size, adding capacity no longer increases performance because the
    VM itself is the bottleneck.  Returns ``None`` when the value never scales
    (zero slope) or is already at the ceiling at size zero.
    """
    per_gib = (
        model.throughput_mibps_per_gib_write if write else model.throughput_mibps_per_gib_read
    ) if throughput else (model.iops_per_gib_write if write else model.iops_per_gib_read)
    base = (
        model.throughput_mibps_base_write if write else model.throughput_mibps_base_read
    ) if throughput else (model.iops_base_write if write else model.iops_base_read)
    cap = (
        (machine_limit.max_write_mibps if write else machine_limit.max_read_mibps)
        if throughput
        else (machine_limit.max_write_iops if write else machine_limit.max_read_iops)
    )
    if per_gib <= 0:
        return None
    delta = cap - base
    if delta <= 0:
        return Decimal(0)
    return delta / per_gib


def saturation_size_gib(
    model: DiskPerformanceModel,
    machine_limit: MachineTypeDiskLimit | None,
    *,
    write: bool = False,
    throughput: bool = False,
) -> Decimal | None:
    """Smallest size at which the value stops increasing (VM *and* type cap reached).

    This is the size an optimizer should buy up to and no further.  Returns
    ``None`` when the value keeps scaling (no cap exists on either layer).
    """
    if throughput:
        per_gib = (
            model.throughput_mibps_per_gib_write if write else model.throughput_mibps_per_gib_read
        )
        base = (
            model.throughput_mibps_base_write if write else model.throughput_mibps_base_read
        )
        type_cap = model.type_cap_mibps(write=write)
        instance_cap = (
            (machine_limit.max_write_mibps if write else machine_limit.max_read_mibps)
            if machine_limit
            else None
        )
    else:
        per_gib = model.iops_per_gib_write if write else model.iops_per_gib_read
        base = model.iops_base_write if write else model.iops_base_read
        type_cap = model.type_cap_iops(write=write)
        instance_cap = (
            (machine_limit.max_write_iops if write else machine_limit.max_read_iops)
            if machine_limit
            else None
        )

    caps = [cap for cap in (instance_cap, type_cap) if cap is not None]
    if not caps:
        return None
    effective = min(caps)
    if base >= effective:
        return Decimal(0)
    if per_gib <= 0:
        return None
    return (effective - base) / per_gib
