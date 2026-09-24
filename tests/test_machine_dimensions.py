"""Machine dimensions (vCPU, memory, network) as constraints and objectives."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from gcp_opt.catalog import Catalog
from gcp_opt.dataset import Dataset
from gcp_opt.models import CostBasis, DiskKind, MachinePrice, MachineTypeInfo, Objective, SourceRef
from gcp_opt.query import (
    Requirement,
    best_machine,
    machine_satisfies,
    min_cost_option,
    optimize,
    rank_configs,
)


def _info(
    *,
    cpus: int | None = 8,
    memory: Decimal | None = Decimal(32),
    network: Decimal | None = Decimal(16),
) -> MachineTypeInfo:
    return MachineTypeInfo(
        name="test-standard-8",
        family="test",
        guest_cpus=cpus,
        memory_gb=memory,
        network_egress_gbps=network,
        source=SourceRef(url="test://machine"),
    )


def test_network_bandwidth_is_populated(catalog: Catalog) -> None:
    n2 = catalog.machine_info("n2-standard-8")
    assert n2 is not None
    assert n2.network_egress_gbps == Decimal(16)
    big = catalog.machine_info("n2-standard-128")
    assert big is not None
    assert big.network_egress_gbps == Decimal(32)
    assert big.network_tier1_egress_gbps == Decimal(100)

    with_network = [
        info for info in catalog.dataset.machine_types.values() if info.network_egress_gbps
    ]
    assert len(with_network) > 400


def test_machine_satisfies_min_and_max() -> None:
    info = _info()
    assert machine_satisfies(info, Requirement.build())[0] is True
    assert machine_satisfies(info, Requirement.build(min_vcpus=4, max_vcpus=16))[0] is True
    assert machine_satisfies(info, Requirement.build(min_vcpus=16))[0] is False
    assert machine_satisfies(info, Requirement.build(max_vcpus=4))[0] is False
    assert machine_satisfies(info, Requirement.build(min_memory_gib="64GiB"))[0] is False
    assert machine_satisfies(info, Requirement.build(min_network_gbps=10))[0] is True
    assert machine_satisfies(info, Requirement.build(min_network_gbps=40))[0] is False


def test_unknown_machine_values_fail_min_but_pass_max() -> None:
    unknown = _info(cpus=None, memory=None, network=None)
    assert machine_satisfies(unknown, Requirement.build(min_vcpus=1))[0] is False
    # A ceiling cannot be violated by an unknown value.
    assert machine_satisfies(unknown, Requirement.build(max_vcpus=1))[0] is True


def test_best_machine_max_memory(catalog: Catalog) -> None:
    result = best_machine(
        catalog, catalog.machine_names(), objective=Objective.MAX_MEMORY
    )
    expected = max(
        info.memory_gb
        for info in catalog.dataset.machine_types.values()
        if info.memory_gb is not None
    )
    assert result.memory_gb == expected
    assert result.disk is None  # machine-only result
    assert result.guest_cpus is not None


def test_best_machine_max_network(catalog: Catalog) -> None:
    result = best_machine(
        catalog, catalog.machine_names(), objective=Objective.MAX_NETWORK
    )
    expected = max(
        info.network_egress_gbps
        for info in catalog.dataset.machine_types.values()
        if info.network_egress_gbps is not None
    )
    assert result.network_egress_gbps == expected


def test_best_machine_respects_constraints(catalog: Catalog) -> None:
    requirement = Requirement.build(min_memory_gib="512GiB", min_network_gbps=10)
    result = best_machine(
        catalog,
        catalog.machine_names(),
        objective=Objective.MAX_VCPUS,
        requirement=requirement,
    )
    assert result.memory_gb is not None
    assert result.memory_gb >= Decimal(512)
    assert result.network_egress_gbps is not None
    assert result.network_egress_gbps >= Decimal(10)


def test_optimize_machine_objective_attaches_disk_when_needed(catalog: Catalog) -> None:
    machine_only = optimize(
        catalog, catalog.machine_names(), objective=Objective.MAX_NETWORK
    )
    assert machine_only.disk is None

    with_disk = optimize(
        catalog,
        catalog.machine_names(),
        objective=Objective.MAX_MEMORY,
        requirement=Requirement.build(min_total_size_gib="10TB"),
    )
    assert with_disk.disk is not None
    assert with_disk.size_gib is not None
    assert with_disk.size_gib >= Decimal("9313")


def test_min_cost_honors_machine_constraints(catalog: Catalog) -> None:
    requirement = Requirement.build(min_total_size_gib="1TB", min_vcpus=16, min_memory_gib="64GiB")
    option = min_cost_option(
        catalog,
        [name for name in catalog.machine_names() if name.startswith("n2-")],
        requirement=requirement,
    )
    info = catalog.machine_info(option.machine_type)
    assert info is not None
    assert info.guest_cpus is not None
    assert info.guest_cpus >= 16
    assert info.memory_gb is not None
    assert info.memory_gb >= Decimal(64)


def test_config_cost_basis_is_disk_only_without_machine_prices(catalog: Catalog) -> None:
    config = catalog.config_option("n2-standard-8", disk_kind=DiskKind.PD_SSD, size_gib=100)
    assert config.cost_basis is CostBasis.DISK_ONLY
    assert config.machine_monthly_cost_usd is None
    assert config.monthly_cost_usd == config.disk_monthly_cost_usd


def test_config_cost_includes_machine_when_priced() -> None:
    dataset = Dataset.load_bundled()
    price = MachinePrice(
        machine_type="n2-standard-8",
        region="us-central1",
        hourly_usd=Decimal("0.5"),
        source=SourceRef(url="test://price"),
    )
    priced = replace(dataset, machine_prices=(price,))
    catalog = Catalog(priced)

    config = catalog.config_option("n2-standard-8", disk_kind=DiskKind.PD_SSD, size_gib=100)
    assert config.cost_basis is CostBasis.MACHINE_AND_DISK
    assert config.machine_monthly_cost_usd == Decimal("0.5") * 730
    assert config.machine_monthly_cost_usd is not None
    assert config.disk_monthly_cost_usd is not None
    assert config.monthly_cost_usd == (
        config.machine_monthly_cost_usd + config.disk_monthly_cost_usd
    )
    assert catalog.machine_hourly_price("n2-standard-8", "us-central1") == Decimal("0.5")
    assert catalog.machine_hourly_price("n2-standard-8", "europe-west1") is None


def test_optimize_min_cost_uses_machine_price() -> None:
    dataset = Dataset.load_bundled()
    # Make the physically smaller n2-standard-4 pricey and n2-standard-8 cheap, so
    # including machine cost flips the choice that disk-only cost cannot see.
    prices = (
        MachinePrice(
            machine_type="n2-standard-4",
            region="us-central1",
            hourly_usd=Decimal("10"),
            source=SourceRef(url="test://price"),
        ),
        MachinePrice(
            machine_type="n2-standard-8",
            region="us-central1",
            hourly_usd=Decimal("0.1"),
            source=SourceRef(url="test://price"),
        ),
    )
    catalog = Catalog(replace(dataset, machine_prices=prices))
    config = optimize(
        catalog,
        ["n2-standard-4", "n2-standard-8"],
        objective=Objective.MIN_COST,
        requirement=Requirement.build(min_total_size_gib="1TB"),
        region="us-central1",
    )
    assert config.cost_basis is CostBasis.MACHINE_AND_DISK
    assert config.machine_type == "n2-standard-8"


def test_optimize_disk_objective_still_works(catalog: Catalog) -> None:
    config = optimize(
        catalog,
        [name for name in catalog.machine_names() if name.startswith("n2-")],
        objective=Objective.MAX_DISK_READ,
        requirement=Requirement.build(min_total_size_gib="10TB"),
        budget_usd=2000,
    )
    assert config.disk is not None
    assert config.read_mibps is not None
    assert config.read_mibps > Decimal(1200)


def test_best_machine_rejects_bad_objective(catalog: Catalog) -> None:
    with pytest.raises(ValueError, match="not a machine objective"):
        best_machine(catalog, ["n2-standard-8"], objective=Objective.MIN_COST)


def test_rank_configs_machine_objective_top_n(catalog: Catalog) -> None:
    configs = rank_configs(
        catalog, catalog.machine_names(), objective=Objective.MAX_MEMORY, top=3
    )
    assert len(configs) == 3
    memories = [config.memory_gb for config in configs]
    assert memories == sorted(memories, reverse=True)  # type: ignore[type-var]
    assert len({config.machine_type for config in configs}) == 3


def test_rank_configs_disk_objective_top_n(catalog: Catalog) -> None:
    configs = rank_configs(
        catalog,
        [name for name in catalog.machine_names() if name.startswith("n2-")],
        objective=Objective.MAX_DISK_READ,
        requirement=Requirement.build(min_total_size_gib="10TB"),
        budget_usd=3000,
        top=3,
    )
    assert 1 < len(configs) <= 3
    reads = [config.read_mibps for config in configs]
    assert reads == sorted(reads, reverse=True)  # type: ignore[type-var]


def test_disk_objective_budget_accounts_for_vm_cost(catalog: Catalog) -> None:
    """A pricey VM leaves less budget for the disk, changing the pairing."""
    machines = ["n2-highcpu-64"]
    requirement = Requirement.build(min_total_size_gib="10TB")
    baseline = optimize(
        catalog,
        machines,
        objective=Objective.MAX_DISK_READ,
        requirement=requirement,
        budget_usd=3000,
    )
    assert baseline.read_mibps == Decimal(4000)  # disk-only budget reaches the ceiling

    price = MachinePrice(
        machine_type="n2-highcpu-64",
        region="us-central1",
        hourly_usd=Decimal("2"),
        source=SourceRef(url="test://price"),
    )
    priced = Catalog(replace(catalog.dataset, machine_prices=(price,)))
    config = optimize(
        priced,
        machines,
        objective=Objective.MAX_DISK_READ,
        requirement=requirement,
        budget_usd=3000,
    )
    assert config.cost_basis is CostBasis.MACHINE_AND_DISK
    assert config.machine_monthly_cost_usd == Decimal("2") * 730
    assert config.monthly_cost_usd is not None
    assert config.monthly_cost_usd <= Decimal(3000)
    assert config.disk is not None
    # The VM eats budget, so the disk can no longer be provisioned to the ceiling.
    assert config.read_mibps is not None
    assert config.read_mibps < baseline.read_mibps


def test_rank_configs_disk_budget_totals_vm_and_disk(catalog: Catalog) -> None:
    price = MachinePrice(
        machine_type="n2-standard-8",
        region="us-central1",
        hourly_usd=Decimal("0.5"),
        source=SourceRef(url="test://price"),
    )
    priced = Catalog(replace(catalog.dataset, machine_prices=(price,)))
    configs = rank_configs(
        priced,
        ["n2-standard-8"],
        objective=Objective.MAX_DISK_READ,
        requirement=Requirement.build(min_total_size_gib="1TB"),
        budget_usd=5000,
        top=1,
    )
    config = configs[0]
    assert config.disk is not None
    assert config.disk_monthly_cost_usd is not None
    assert config.machine_monthly_cost_usd == Decimal(365)
    assert config.monthly_cost_usd == config.machine_monthly_cost_usd + config.disk_monthly_cost_usd
