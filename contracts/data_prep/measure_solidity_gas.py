#!/usr/bin/env python3
"""Compile and benchmark SurplusSharingAccounting in an in-memory EVM.

Outputs a CSV compatible with hook_replay.py:
    event_id,transfer_solidity_wad,gas_used,cap_binds

The reported gas_used is measured inside EVM execution with gasleft(), around
only the call to computeTransfer. It therefore excludes deployment gas,
transaction intrinsic gas, and most outer ABI/transaction overhead.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

SOLC_VERSION = "0.8.24"

BENCHMARK_SOURCE = r'''
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "./SurplusSharingAccounting.sol";

contract SurplusSharingBenchmark {
    SurplusSharingAccounting public immutable accounting;

    constructor(address accountingAddress) {
        accounting = SurplusSharingAccounting(accountingAddress);
    }

    function benchmarkTransfer(
        uint256 surplusHat,
        uint256 executionMarginHat,
        uint256 lambdaWad,
        uint256 gammaWad,
        uint256 delta
    ) external view returns (
        uint256 transferAmount,
        bool capBinds,
        uint256 gasUsed
    ) {
        uint256 gasBefore = gasleft();
        (transferAmount, capBinds) = accounting.computeTransfer(
            surplusHat,
            executionMarginHat,
            lambdaWad,
            gammaWad,
            delta
        );
        gasUsed = gasBefore - gasleft();
    }
}
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path("../src/SurplusSharingAccounting.sol"),
        help="Path to SurplusSharingAccounting.sol",
    )
    parser.add_argument(
        "--vectors",
        type=Path,
        default=Path("../results/test_vectors.json"),
        help="Path to test_vectors.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("../results/solidity_gas.csv"),
        help="Output CSV path",
    )
    parser.add_argument(
        "--solc-version",
        default=SOLC_VERSION,
        help=f"Solidity compiler version (default: {SOLC_VERSION})",
    )
    parser.add_argument(
        "--optimizer-runs",
        type=int,
        default=200,
        help="Solidity optimizer runs (default: 200)",
    )
    return parser.parse_args()


def load_dependencies() -> tuple[Any, Any, Any, Any]:
    try:
        import solcx
        from eth_tester import EthereumTester, PyEVMBackend
        from web3 import Web3
        from web3.providers.eth_tester import EthereumTesterProvider
    except ImportError as exc:
        print(
            "Missing dependency. Install with:\n"
            "  python -m pip install py-solc-x web3 eth-tester py-evm\n",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    return solcx, EthereumTester, PyEVMBackend, (Web3, EthereumTesterProvider)


def compile_contracts(
    solcx: Any,
    contract_path: Path,
    solc_version: str,
    optimizer_runs: int,
) -> dict[str, Any]:
    if not contract_path.exists():
        raise FileNotFoundError(f"Contract not found: {contract_path}")

    installed = {str(v) for v in solcx.get_installed_solc_versions()}
    if solc_version not in installed:
        print(f"Installing solc {solc_version}...")
        solcx.install_solc(solc_version)

    source_name = "SurplusSharingAccounting.sol"
    benchmark_name = "SurplusSharingBenchmark.sol"

    standard_input = {
        "language": "Solidity",
        "sources": {
            source_name: {"content": contract_path.read_text(encoding="utf-8")},
            benchmark_name: {"content": BENCHMARK_SOURCE},
        },
        "settings": {
            "optimizer": {"enabled": True, "runs": optimizer_runs},
            "outputSelection": {
                "*": {"*": ["abi", "evm.bytecode.object"]}
            },
        },
    }

    return solcx.compile_standard(
        standard_input,
        solc_version=solc_version,
        allow_paths=str(contract_path.parent.resolve()),
    )


def artifact(compiled: dict[str, Any], source: str, contract: str) -> tuple[list[Any], str]:
    item = compiled["contracts"][source][contract]
    abi = item["abi"]
    bytecode = item["evm"]["bytecode"]["object"]
    if not bytecode:
        raise ValueError(f"No bytecode produced for {contract}")
    return abi, bytecode


def deploy(w3: Any, abi: list[Any], bytecode: str, constructor_args: tuple[Any, ...] = ()) -> Any:
    account = w3.eth.accounts[0]
    factory = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx_hash = factory.constructor(*constructor_args).transact({"from": account})
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt.status != 1:
        raise RuntimeError("Contract deployment reverted")
    return w3.eth.contract(address=receipt.contractAddress, abi=abi)


def as_uint(vector: dict[str, Any], key: str) -> int:
    try:
        value = int(vector[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid or missing {key!r} in vector {vector!r}") from exc
    if value < 0:
        raise ValueError(f"{key} must be nonnegative")
    return value


def main() -> None:
    args = parse_args()
    solcx, EthereumTester, PyEVMBackend, web3_items = load_dependencies()
    Web3, EthereumTesterProvider = web3_items

    if not args.vectors.exists():
        raise FileNotFoundError(f"Vector file not found: {args.vectors}")

    payload = json.loads(args.vectors.read_text(encoding="utf-8"))
    vectors = payload.get("vectors")
    if not isinstance(vectors, list):
        raise ValueError("test_vectors.json must contain a 'vectors' list")
    declared_count = payload.get("count")
    if declared_count is not None and int(declared_count) != len(vectors):
        raise ValueError(
            f"Vector count mismatch: header says {declared_count}, found {len(vectors)}"
        )

    compiled = compile_contracts(
        solcx,
        args.contract,
        args.solc_version,
        args.optimizer_runs,
    )
    accounting_abi, accounting_bytecode = artifact(
        compiled, "SurplusSharingAccounting.sol", "SurplusSharingAccounting"
    )
    benchmark_abi, benchmark_bytecode = artifact(
        compiled, "SurplusSharingBenchmark.sol", "SurplusSharingBenchmark"
    )

    tester = EthereumTester(backend=PyEVMBackend())
    w3 = Web3(EthereumTesterProvider(tester))
    accounting = deploy(w3, accounting_abi, accounting_bytecode)
    benchmark = deploy(
        w3,
        benchmark_abi,
        benchmark_bytecode,
        (accounting.address,),
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    mismatches: list[str] = []

    for index, vector in enumerate(vectors):
        event_id = str(vector.get("event_id", index))
        inputs = (
            as_uint(vector, "surplus_hat_wad"),
            as_uint(vector, "execution_margin_hat_wad"),
            as_uint(vector, "lambda_wad"),
            as_uint(vector, "gamma_wad"),
            as_uint(vector, "delta_wad"),
        )
        expected = as_uint(vector, "expected_transfer_wad")

        transfer, cap_binds, gas_used = benchmark.functions.benchmarkTransfer(
            *inputs
        ).call({"from": w3.eth.accounts[0]})

        transfer = int(transfer)
        gas_used = int(gas_used)
        if transfer != expected:
            mismatches.append(
                f"{event_id}: Solidity={transfer}, Python={expected}, "
                f"abs_error={abs(transfer - expected)}"
            )

        rows.append(
            {
                "event_id": event_id,
                "transfer_solidity_wad": transfer,
                "gas_used": gas_used,
                "cap_binds": 1 if cap_binds else 0,
            }
        )

    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "event_id",
                "transfer_solidity_wad",
                "gas_used",
                "cap_binds",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    gas_values = sorted(int(row["gas_used"]) for row in rows)
    median = gas_values[len(gas_values) // 2] if gas_values else 0
    p95_index = max(0, min(len(gas_values) - 1, int(0.95 * len(gas_values)) - 1))
    p95 = gas_values[p95_index] if gas_values else 0

    print(f"Compiler: solc {args.solc_version}, optimizer runs={args.optimizer_runs}")
    print(f"Vectors executed: {len(rows)}")
    print(f"Output: {args.output}")
    print(f"Median measured gas: {median}")
    print(f"P95 measured gas: {p95}")
    print(f"Maximum measured gas: {max(gas_values) if gas_values else 0}")
    print(f"Python/Solidity mismatches: {len(mismatches)}")

    if mismatches:
        print("\nFirst mismatches:", file=sys.stderr)
        for message in mismatches[:10]:
            print(f"  {message}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
