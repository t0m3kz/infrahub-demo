#!/usr/bin/env python3
"""DC11 Cabling Test Runner

This script runs comprehensive cabling tests for DC11 scenario against a live Infrahub instance.

Usage:
    # Run all DC11 tests
    uv run python scripts/test_dc11_cabling.py

    # Run with verbose output
    uv run python scripts/test_dc11_cabling.py --verbose

    # Run specific test class
    uv run python scripts/test_dc11_cabling.py --test-class TestDC11CablingGeneration

Prerequisites:
    - Infrahub running at http://localhost:8000
    - DC11 scenario loaded: uv run infrahubctl object load data/demos/01_data_center/dc11
    - Generators executed for DC11
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest


def main() -> int:
    """Run DC11 cabling integration tests."""
    parser = argparse.ArgumentParser(
        description="Run DC11 cabling integration tests",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose test output"
    )
    parser.add_argument(
        "--test-class",
        type=str,
        help="Run specific test class (e.g., TestDC11CablingStructure)",
    )
    parser.add_argument(
        "--test-method",
        type=str,
        help="Run specific test method (e.g., test_dc11_datacenter_exists)",
    )
    parser.add_argument("--failfast", action="store_true", help="Stop on first failure")

    args = parser.parse_args()

    # Build pytest arguments
    test_file = (
        Path(__file__).parent.parent / "tests" / "integration" / "test_dc11_cabling.py"
    )
    pytest_args = [str(test_file)]

    if args.verbose:
        pytest_args.append("-vv")
    else:
        pytest_args.append("-v")

    if args.failfast:
        pytest_args.append("-x")

    # Add asyncio mode
    pytest_args.append("--asyncio-mode=auto")

    # Filter by test class or method
    if args.test_class:
        pytest_args.append(f"-k={args.test_class}")
    elif args.test_method:
        pytest_args.append(f"-k={args.test_method}")

    # Add color and summary
    pytest_args.extend(["--color=yes", "--tb=short"])

    print("=" * 80)
    print("DC11 Cabling Integration Test Suite")
    print("=" * 80)
    print(f"Test file: {test_file}")
    print(f"Arguments: {' '.join(pytest_args)}")
    print("=" * 80)
    print()

    # Run tests
    exit_code = pytest.main(pytest_args)

    print()
    print("=" * 80)
    if exit_code == 0:
        print("✅ All DC11 cabling tests PASSED")
    else:
        print("❌ Some DC11 cabling tests FAILED")
    print("=" * 80)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
