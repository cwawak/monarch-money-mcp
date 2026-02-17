#!/usr/bin/env python3
"""MonarchMoney MCP Server - Provides access to Monarch Money financial data via MCP protocol."""

import os
import asyncio
import json
import sys
import re
import calendar
from typing import Any, Dict, Optional, List
from datetime import datetime, date
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.models import InitializationOptions
from mcp.types import ServerCapabilities
from mcp.types import Tool, TextContent
from monarchmoney import MonarchMoney
from monarchmoney.monarchmoney import MonarchMoneyEndpoints
from gql import gql

# Monarch migrated API host; keep overrideable for troubleshooting.
MonarchMoneyEndpoints.BASE_URL = os.getenv(
    "MONARCH_API_BASE_URL",
    "https://api.monarch.com",
).rstrip("/")


def convert_dates_to_strings(obj: Any) -> Any:
    """
    Recursively convert all date/datetime objects to ISO format strings.
    
    This ensures that the data can be serialized by any JSON encoder,
    not just our custom one. This is necessary because the MCP framework
    may attempt to serialize the response before we can use our custom encoder.
    """
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {key: convert_dates_to_strings(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [convert_dates_to_strings(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(convert_dates_to_strings(item) for item in obj)
    else:
        return obj


def normalize_mfa_secret(secret: Optional[str]) -> Optional[str]:
    """Normalize a TOTP secret and reject obviously invalid values."""
    if not secret:
        return None

    cleaned = re.sub(r"[\s-]+", "", secret).upper()
    if not cleaned:
        return None

    if not re.fullmatch(r"[A-Z2-7=]+", cleaned):
        return None

    return cleaned


def merge_id_filters(plural_value: Any, singular_value: Optional[str]) -> List[str]:
    """Merge singular/plural ID filters into a de-duplicated list."""
    merged_ids: List[str] = []

    if isinstance(plural_value, list):
        merged_ids.extend(
            item for item in plural_value
            if isinstance(item, str) and item
        )
    elif isinstance(plural_value, str) and plural_value:
        merged_ids.append(plural_value)

    if isinstance(singular_value, str) and singular_value:
        merged_ids.append(singular_value)

    return list(dict.fromkeys(merged_ids))


TRANSACTION_BOOLEAN_FILTER_KEYS = (
    "has_attachments",
    "has_notes",
    "hidden_from_reports",
    "is_split",
    "is_recurring",
    "imported_from_mint",
    "synced_from_institution",
)


def build_transaction_filters(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Build upstream transaction filters supported by monarchmoney."""
    filters: Dict[str, Any] = {}

    has_start_date = "start_date" in arguments
    has_end_date = "end_date" in arguments
    if has_start_date != has_end_date:
        raise ValueError("You must specify both start_date and end_date, not just one of them.")

    if "start_date" in arguments:
        filters["start_date"] = datetime.strptime(
            arguments["start_date"], "%Y-%m-%d"
        ).date().isoformat()
    if "end_date" in arguments:
        filters["end_date"] = datetime.strptime(
            arguments["end_date"], "%Y-%m-%d"
        ).date().isoformat()
    if "search" in arguments:
        filters["search"] = arguments["search"]

    account_ids = merge_id_filters(
        arguments.get("account_ids"),
        arguments.get("account_id"),
    )
    if account_ids:
        filters["account_ids"] = account_ids

    category_ids = merge_id_filters(
        arguments.get("category_ids"),
        arguments.get("category_id"),
    )
    if category_ids:
        filters["category_ids"] = category_ids

    tag_ids = merge_id_filters(arguments.get("tag_ids"), None)
    if tag_ids:
        filters["tag_ids"] = tag_ids

    for key in TRANSACTION_BOOLEAN_FILTER_KEYS:
        if key in arguments:
            filters[key] = arguments[key]

    return filters


def apply_transaction_post_filters(
    transactions: Any,
    merchant_id: Optional[str],
    amount_min: Optional[float],
    amount_max: Optional[float],
) -> Any:
    """
    Apply local post-fetch transaction filtering for fields not supported upstream.

    Current post-filters:
    - merchant_id
    - amount_min
    - amount_max
    """
    if merchant_id is None and amount_min is None and amount_max is None:
        return transactions

    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        raise ValueError("amount_min cannot be greater than amount_max")

    if not isinstance(transactions, dict):
        return transactions

    all_transactions = transactions.get("allTransactions")
    if not isinstance(all_transactions, dict):
        return transactions

    results = all_transactions.get("results")
    if not isinstance(results, list):
        return transactions

    def matches_post_filters(txn: Any) -> bool:
        if not isinstance(txn, dict):
            return False

        if merchant_id:
            merchant = txn.get("merchant")
            merchant_value = merchant.get("id") if isinstance(merchant, dict) else None
            if merchant_value != merchant_id:
                return False

        amount = txn.get("amount")
        if amount_min is not None:
            if not isinstance(amount, (int, float)) or float(amount) < amount_min:
                return False
        if amount_max is not None:
            if not isinstance(amount, (int, float)) or float(amount) > amount_max:
                return False

        return True

    filtered_results = [txn for txn in results if matches_post_filters(txn)]
    updated_transactions = dict(transactions)
    updated_all_transactions = dict(all_transactions)
    updated_all_transactions["results"] = filtered_results
    updated_all_transactions["totalCount"] = len(filtered_results)
    updated_transactions["allTransactions"] = updated_all_transactions
    return updated_transactions


def maybe_strip_transaction_rules(transactions: Any, include_transaction_rules: bool) -> Any:
    """Drop heavy transactionRules payload by default to reduce MCP output size."""
    if include_transaction_rules:
        return transactions

    if (
        isinstance(transactions, dict)
        and isinstance(transactions.get("transactionRules"), list)
    ):
        slim_transactions = dict(transactions)
        slim_transactions.pop("transactionRules", None)
        return slim_transactions

    return transactions


def build_compact_transaction_results(transactions: Any) -> Dict[str, Any]:
    """Project Monarch transaction payload into concise agent-friendly rows."""
    if not isinstance(transactions, dict):
        return {"totalCount": 0, "returnedCount": 0, "results": []}

    all_transactions = transactions.get("allTransactions")
    if not isinstance(all_transactions, dict):
        return {"totalCount": 0, "returnedCount": 0, "results": []}

    results = all_transactions.get("results")
    if not isinstance(results, list):
        results = []

    compact_results: List[Dict[str, Any]] = []
    for txn in results:
        if not isinstance(txn, dict):
            continue

        merchant = txn.get("merchant") if isinstance(txn.get("merchant"), dict) else {}
        category = txn.get("category") if isinstance(txn.get("category"), dict) else {}
        account = txn.get("account") if isinstance(txn.get("account"), dict) else {}

        compact_results.append({
            "id": txn.get("id"),
            "date": txn.get("date"),
            "amount": txn.get("amount"),
            "merchant_id": merchant.get("id"),
            "merchant_name": merchant.get("name"),
            "plaid_name": txn.get("plaidName"),
            "category_id": category.get("id"),
            "category_name": category.get("name"),
            "account_id": account.get("id"),
            "account_name": account.get("displayName"),
            "is_recurring": txn.get("isRecurring"),
            "pending": txn.get("pending"),
            "notes": txn.get("notes"),
        })

    total_count = all_transactions.get("totalCount")
    if not isinstance(total_count, int):
        total_count = len(compact_results)

    return {
        "totalCount": total_count,
        "returnedCount": len(compact_results),
        "results": compact_results,
    }


# Initialize the MCP server
server = Server("monarch-money")

# Global variable to store the MonarchMoney client
mm_client: Optional[MonarchMoney] = None
mm_init_error: Optional[str] = None
session_file = Path.home() / ".monarchmoney_session"


def is_auth_error(exc: Exception) -> bool:
    """Best-effort detection of Monarch auth/session expiration errors."""
    msg = str(exc).lower()
    markers = [
        "401",
        "unauthorized",
        "http code 401",
        "token",
        "session",
        "timed out",
        "timeout",
    ]
    return any(marker in msg for marker in markers)


async def refresh_client_session() -> None:
    """Force a fresh client initialization for auth recovery."""
    global mm_client, mm_init_error
    mm_client = None
    mm_init_error = None

    # Drop stale cached session before re-auth to avoid looping on invalid token.
    if session_file.exists():
        try:
            session_file.unlink()
        except Exception:
            pass

    await initialize_client()


async def initialize_client():
    """Initialize the MonarchMoney client with authentication."""
    global mm_client, mm_init_error
    
    email = os.getenv("MONARCH_EMAIL")
    password = os.getenv("MONARCH_PASSWORD")
    raw_mfa_secret = os.getenv("MONARCH_MFA_SECRET")
    mfa_secret = normalize_mfa_secret(raw_mfa_secret)
    
    if not email or not password:
        mm_client = None
        mm_init_error = "MONARCH_EMAIL and MONARCH_PASSWORD environment variables are required"
        return
    
    mm_client = MonarchMoney()
    
    # Try to load existing session first
    if session_file.exists() and not os.getenv("MONARCH_FORCE_LOGIN"):
        try:
            mm_client.load_session(str(session_file))
            # Test if session is still valid
            await mm_client.get_accounts()
            print("Loaded existing session successfully", file=sys.stderr)
            mm_init_error = None
            return
        except Exception:
            print("Existing session invalid, logging in fresh", file=sys.stderr)
            # Reset client so stale auth headers from an invalid session
            # are not sent during fresh login.
            mm_client = MonarchMoney()
    
    if raw_mfa_secret and not mfa_secret:
        print(
            "Ignoring MONARCH_MFA_SECRET because it is not a valid base32 secret",
            file=sys.stderr,
        )

    # Login with credentials
    if mfa_secret:
        try:
            await mm_client.login(
                email,
                password,
                use_saved_session=False,
                save_session=False,
                mfa_secret_key=mfa_secret,
            )
        except Exception as e:
            if "Non-base32 digit found" not in str(e):
                raise
            print(
                "MONARCH_MFA_SECRET failed base32 parsing; retrying without MFA secret",
                file=sys.stderr,
            )
            await mm_client.login(
                email,
                password,
                use_saved_session=False,
                save_session=False,
            )
    else:
        await mm_client.login(
            email,
            password,
            use_saved_session=False,
            save_session=False,
        )
    
    # Save session for future use
    mm_client.save_session(str(session_file))
    print("Logged in and saved session", file=sys.stderr)
    mm_init_error = None


async def ensure_client_initialized() -> Optional[str]:
    """Lazily initialize the client and return an error message if unavailable."""
    global mm_client, mm_init_error
    if mm_client is not None:
        return None

    try:
        await initialize_client()
    except Exception as e:
        mm_client = None
        mm_init_error = str(e)

    return mm_init_error


def resolve_budget_date_range(start_date: Optional[str], end_date: Optional[str]) -> Dict[str, str]:
    """Resolve budget range to explicit ISO dates, matching upstream defaults."""
    if bool(start_date) != bool(end_date):
        raise ValueError("You must specify both start_date and end_date, not just one of them.")

    if start_date and end_date:
        return {"startDate": start_date, "endDate": end_date}

    today = datetime.today()

    last_month = today.month - 1
    last_month_year = today.year
    if last_month < 1:
        last_month_year -= 1
        last_month = 12
    resolved_start = datetime(last_month_year, last_month, 1).strftime("%Y-%m-%d")

    next_month = today.month + 1
    next_month_year = today.year
    if next_month > 12:
        next_month_year += 1
        next_month = 1
    last_day_of_next_month = calendar.monthrange(next_month_year, next_month)[1]
    resolved_end = datetime(next_month_year, next_month, last_day_of_next_month).strftime("%Y-%m-%d")

    return {"startDate": resolved_start, "endDate": resolved_end}


async def get_budgets_lite(start_date: Optional[str], end_date: Optional[str]) -> Dict[str, Any]:
    """Query budget data without unstable top-level fields that currently error."""
    if not mm_client:
        raise RuntimeError("MonarchMoney client not initialized")

    variables = resolve_budget_date_range(start_date, end_date)
    query = gql(
        """
        query GetBudgetDataLite($startDate: Date!, $endDate: Date!) {
          budgetData(startMonth: $startDate, endMonth: $endDate) {
            monthlyAmountsByCategory {
              category {
                id
                __typename
              }
              monthlyAmounts {
                month
                plannedCashFlowAmount
                plannedSetAsideAmount
                actualAmount
                remainingAmount
                previousMonthRolloverAmount
                rolloverType
                __typename
              }
              __typename
            }
            monthlyAmountsByCategoryGroup {
              categoryGroup {
                id
                __typename
              }
              monthlyAmounts {
                month
                plannedCashFlowAmount
                actualAmount
                remainingAmount
                previousMonthRolloverAmount
                rolloverType
                __typename
              }
              __typename
            }
            monthlyAmountsForFlexExpense {
              budgetVariability
              monthlyAmounts {
                month
                plannedCashFlowAmount
                actualAmount
                remainingAmount
                previousMonthRolloverAmount
                rolloverType
                __typename
              }
              __typename
            }
            totalsByMonth {
              month
              totalIncome {
                plannedAmount
                actualAmount
                remainingAmount
                previousMonthRolloverAmount
                __typename
              }
              totalExpenses {
                plannedAmount
                actualAmount
                remainingAmount
                previousMonthRolloverAmount
                __typename
              }
              totalFixedExpenses {
                plannedAmount
                actualAmount
                remainingAmount
                previousMonthRolloverAmount
                __typename
              }
              totalNonMonthlyExpenses {
                plannedAmount
                actualAmount
                remainingAmount
                previousMonthRolloverAmount
                __typename
              }
              totalFlexibleExpenses {
                plannedAmount
                actualAmount
                remainingAmount
                previousMonthRolloverAmount
                __typename
              }
              __typename
            }
            __typename
          }
        }
        """
    )

    budget_payload = await mm_client.gql_call(
        operation="GetBudgetDataLite",
        graphql_query=query,
        variables=variables,
    )

    budget_data = budget_payload.get("budgetData", {}) if isinstance(budget_payload, dict) else {}

    categories_by_id: Dict[str, Dict[str, Any]] = {}
    groups_by_id: Dict[str, Dict[str, Any]] = {}
    try:
        categories_payload = await mm_client.get_transaction_categories()
        categories = categories_payload.get("categories", []) if isinstance(categories_payload, dict) else []
        for cat in categories:
            cat_id = cat.get("id")
            group = cat.get("group") or {}
            if cat_id:
                categories_by_id[cat_id] = {
                    "id": cat_id,
                    "name": cat.get("name"),
                    "group": {
                        "id": group.get("id"),
                        "name": group.get("name"),
                        "type": group.get("type"),
                    },
                }
            group_id = group.get("id")
            if group_id and group_id not in groups_by_id:
                groups_by_id[group_id] = {
                    "id": group_id,
                    "name": group.get("name"),
                    "type": group.get("type"),
                }
    except Exception:
        # Budget payload is still useful even if category metadata lookup fails.
        categories_by_id = {}
        groups_by_id = {}

    for row in budget_data.get("monthlyAmountsByCategory", []) if isinstance(budget_data, dict) else []:
        category = row.get("category") or {}
        category_id = category.get("id")
        meta = categories_by_id.get(category_id)
        if meta:
            row["category"] = {
                **category,
                "name": meta.get("name"),
                "group": meta.get("group"),
            }

    for row in budget_data.get("monthlyAmountsByCategoryGroup", []) if isinstance(budget_data, dict) else []:
        group = row.get("categoryGroup") or {}
        group_id = group.get("id")
        meta = groups_by_id.get(group_id)
        if meta:
            row["categoryGroup"] = {
                **group,
                "name": meta.get("name"),
                "type": meta.get("type"),
            }

    return {
        "startDate": variables["startDate"],
        "endDate": variables["endDate"],
        "budgetData": budget_data,
        "source": "GetBudgetDataLite",
    }


# Tool definitions
@server.list_tools()
async def list_tools() -> List[Tool]:
    """List all available tools."""
    return [
        Tool(
            name="get_accounts",
            description=(
                "List financial accounts with balances and metadata. "
                "Use this to answer account balance questions and to find account IDs for transaction filters."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False
            }
        ),
        Tool(
            name="get_transactions",
            description=(
                "Fetch transactions with full metadata and comprehensive filtering. "
                "Use this for detailed analysis, exports, or workflows that require raw transaction fields. "
                "Returns larger payloads than search_transactions. "
                "Date rule: provide both start_date and end_date, or omit both."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Maximum number of transactions to return per page. "
                            "Use smaller limits for quick checks and higher limits for broader analysis."
                        ),
                        "minimum": 1,
                        "default": 100
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of transactions to skip for pagination.",
                        "minimum": 0,
                        "default": 0
                    },
                    "start_date": {
                        "type": "string",
                        "description": (
                            "Start date in YYYY-MM-DD format. "
                            "Optional, but if provided then end_date is also required."
                        )
                    },
                    "end_date": {
                        "type": "string",
                        "description": (
                            "End date in YYYY-MM-DD format. "
                            "Optional, but if provided then start_date is also required."
                        )
                    },
                    "search": {
                        "type": "string",
                        "description": (
                            "Text search sent to Monarch for merchant/transaction matching. "
                            "Example: 'youtube' often matches 'GOOGLE *YouTube TV'."
                        )
                    },
                    "account_id": {
                        "type": "string",
                        "description": (
                            "Filter by one account ID (backwards-compatible singular form). "
                            "Get account IDs from get_accounts."
                        )
                    },
                    "account_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Filter by one or more account IDs. "
                            "Get account IDs from get_accounts."
                        )
                    },
                    "category_id": {
                        "type": "string",
                        "description": (
                            "Filter by one category ID (backwards-compatible singular form). "
                            "Use category IDs from get_transaction_categories; names are not accepted."
                        )
                    },
                    "category_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Filter by one or more category IDs. "
                            "Use IDs from get_transaction_categories; category names are not accepted."
                        )
                    },
                    "tag_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Filter by one or more transaction tag IDs from get_transaction_tags."
                        )
                    },
                    "has_attachments": {
                        "type": "boolean",
                        "description": "Upstream boolean filter for attachment presence."
                    },
                    "has_notes": {
                        "type": "boolean",
                        "description": "Upstream boolean filter for note presence."
                    },
                    "hidden_from_reports": {
                        "type": "boolean",
                        "description": "Upstream boolean filter for hide-from-reports status."
                    },
                    "is_split": {
                        "type": "boolean",
                        "description": "Upstream boolean filter for split-transaction status."
                    },
                    "is_recurring": {
                        "type": "boolean",
                        "description": "Upstream boolean filter for recurring status."
                    },
                    "imported_from_mint": {
                        "type": "boolean",
                        "description": "Upstream boolean filter for imported-from-Mint status."
                    },
                    "synced_from_institution": {
                        "type": "boolean",
                        "description": "Upstream boolean filter for institution-sync status."
                    },
                    "merchant_id": {
                        "type": "string",
                        "description": (
                            "Post-fetch filter: keep only transactions whose merchant.id matches this value. "
                            "Applied after page fetch, so counts are page-local."
                        )
                    },
                    "amount_min": {
                        "type": "number",
                        "description": (
                            "Post-fetch filter: keep only transactions with amount >= this value. "
                            "Applied after page fetch, so counts are page-local."
                        )
                    },
                    "amount_max": {
                        "type": "number",
                        "description": (
                            "Post-fetch filter: keep only transactions with amount <= this value. "
                            "Applied after page fetch, so counts are page-local."
                        )
                    },
                    "include_transaction_rules": {
                        "type": "boolean",
                        "description": (
                            "Include heavy transactionRules payload in the response. "
                            "Defaults to false to reduce output size."
                        ),
                        "default": False
                    }
                },
                "additionalProperties": False
            }
        ),
        Tool(
            name="search_transactions",
            description=(
                "Quick transaction search with concise projected rows for lower token usage. "
                "Use this for merchant lookups, spending-on-X questions, and exploratory queries. "
                "Returns essential fields only; use get_transactions or get_transaction_details for full metadata. "
                "Date rule: provide both start_date and end_date, or omit both."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": (
                            "Search text sent to Monarch for merchant/transaction matching. "
                            "Example: 'youtube' often matches 'GOOGLE *YouTube TV'."
                        )
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Maximum number of transactions to fetch before concise projection."
                        ),
                        "minimum": 1,
                        "default": 25
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of transactions to skip for pagination.",
                        "minimum": 0,
                        "default": 0
                    },
                    "start_date": {
                        "type": "string",
                        "description": (
                            "Start date in YYYY-MM-DD format. "
                            "Optional, but if provided then end_date is also required."
                        )
                    },
                    "end_date": {
                        "type": "string",
                        "description": (
                            "End date in YYYY-MM-DD format. "
                            "Optional, but if provided then start_date is also required."
                        )
                    },
                    "account_id": {
                        "type": "string",
                        "description": (
                            "Filter by one account ID (backwards-compatible singular form). "
                            "Get account IDs from get_accounts."
                        )
                    },
                    "account_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Filter by one or more account IDs. "
                            "Get account IDs from get_accounts."
                        )
                    },
                    "category_id": {
                        "type": "string",
                        "description": (
                            "Filter by one category ID (backwards-compatible singular form). "
                            "Use category IDs from get_transaction_categories; names are not accepted."
                        )
                    },
                    "category_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Filter by one or more category IDs. "
                            "Use IDs from get_transaction_categories; category names are not accepted."
                        )
                    },
                    "tag_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Filter by one or more transaction tag IDs from get_transaction_tags."
                        )
                    },
                    "has_attachments": {
                        "type": "boolean",
                        "description": "Upstream boolean filter for attachment presence."
                    },
                    "has_notes": {
                        "type": "boolean",
                        "description": "Upstream boolean filter for note presence."
                    },
                    "hidden_from_reports": {
                        "type": "boolean",
                        "description": "Upstream boolean filter for hide-from-reports status."
                    },
                    "is_split": {
                        "type": "boolean",
                        "description": "Upstream boolean filter for split-transaction status."
                    },
                    "is_recurring": {
                        "type": "boolean",
                        "description": "Upstream boolean filter for recurring status."
                    },
                    "imported_from_mint": {
                        "type": "boolean",
                        "description": "Upstream boolean filter for imported-from-Mint status."
                    },
                    "synced_from_institution": {
                        "type": "boolean",
                        "description": "Upstream boolean filter for institution-sync status."
                    },
                    "merchant_id": {
                        "type": "string",
                        "description": (
                            "Post-fetch filter: keep only transactions whose merchant.id matches this value. "
                            "Applied after page fetch, so counts are page-local."
                        )
                    },
                    "amount_min": {
                        "type": "number",
                        "description": (
                            "Post-fetch filter: keep only transactions with amount >= this value. "
                            "Applied after page fetch, so counts are page-local."
                        )
                    },
                    "amount_max": {
                        "type": "number",
                        "description": (
                            "Post-fetch filter: keep only transactions with amount <= this value. "
                            "Applied after page fetch, so counts are page-local."
                        )
                    },
                    "include_transaction_rules": {
                        "type": "boolean",
                        "description": (
                            "Include heavy transactionRules in the payload (usually unnecessary). "
                            "Defaults to false to keep responses smaller."
                        ),
                        "default": False
                    },
                    "include_raw": {
                        "type": "boolean",
                        "description": (
                            "Include the raw upstream payload alongside concise projected rows."
                        ),
                        "default": False
                    }
                },
                "required": ["search"],
                "additionalProperties": False
            }
        ),
        Tool(
            name="get_budgets",
            description=(
                "Get budget data with planned vs actual spending by category. "
                "Use this for questions like 'am I on budget' or 'how much is left in this category'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "Start date in YYYY-MM-DD format"
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date in YYYY-MM-DD format"
                    }
                },
                "additionalProperties": False
            }
        ),
        Tool(
            name="get_cashflow",
            description=(
                "Get cashflow data for income and expense analysis over time. "
                "Use this for trend and period-over-period cashflow questions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "Start date in YYYY-MM-DD format"
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date in YYYY-MM-DD format"
                    }
                },
                "additionalProperties": False
            }
        ),
        Tool(
            name="get_transaction_categories",
            description=(
                "List transaction categories and their IDs. "
                "Use this before category filters because transaction tools expect category IDs, not category names."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False
            }
        ),
        Tool(
            name="get_transaction_details",
            description=(
                "Get full details for one transaction by transaction_id. "
                "Use this after search results when the user asks for deeper transaction context."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "transaction_id": {
                        "type": "string",
                        "description": "ID of the transaction to fetch"
                    },
                    "redirect_posted": {
                        "type": "boolean",
                        "description": "If true, redirect pending transactions to posted counterpart when available",
                        "default": True
                    }
                },
                "required": ["transaction_id"],
                "additionalProperties": False
            }
        ),
        Tool(
            name="get_transaction_tags",
            description=(
                "List available transaction tags and IDs. "
                "Use this to discover tag_ids for transaction filtering."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False
            }
        ),
        Tool(
            name="create_transaction",
            description=(
                "Create a manual transaction entry. "
                "Use negative amounts for expenses and positive amounts for inflows."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "amount": {
                        "type": "number",
                        "description": "Transaction amount (negative for expenses)"
                    },
                    "description": {
                        "type": "string",
                        "description": "Transaction description"
                    },
                    "category_id": {
                        "type": "string",
                        "description": "Category ID for the transaction"
                    },
                    "account_id": {
                        "type": "string",
                        "description": "Account ID for the transaction"
                    },
                    "date": {
                        "type": "string",
                        "description": "Transaction date in YYYY-MM-DD format"
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional notes for the transaction"
                    }
                },
                "required": ["amount", "description", "account_id", "date"],
                "additionalProperties": False
            }
        ),
        Tool(
            name="update_transaction",
            description=(
                "Update fields on an existing transaction by ID. "
                "Use this for corrections to amount, description, category, date, or notes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "transaction_id": {
                        "type": "string",
                        "description": "ID of the transaction to update"
                    },
                    "amount": {
                        "type": "number",
                        "description": "New transaction amount"
                    },
                    "description": {
                        "type": "string",
                        "description": "New transaction description"
                    },
                    "category_id": {
                        "type": "string",
                        "description": "New category ID"
                    },
                    "date": {
                        "type": "string",
                        "description": "New transaction date in YYYY-MM-DD format"
                    },
                    "notes": {
                        "type": "string",
                        "description": "New notes for the transaction"
                    }
                },
                "required": ["transaction_id"],
                "additionalProperties": False
            }
        ),
        Tool(
            name="refresh_accounts",
            description=(
                "Request a refresh of linked accounts from financial institutions. "
                "Use this when recent transactions or balances may not be synced yet."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False
            }
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    """Execute a tool and return the results."""
    arguments = dict(arguments or {})
    internal_retry = bool(arguments.pop("__auth_retry", False))

    init_error = await ensure_client_initialized()
    if init_error or not mm_client:
        return [TextContent(
            type="text",
            text=(
                "Error: MonarchMoney client not initialized. "
                f"Authentication/connection failed: {init_error or 'unknown error'}"
            ),
        )]
    
    try:
        if name == "get_accounts":
            accounts = await mm_client.get_accounts()
            # Convert date objects to strings before serialization
            accounts = convert_dates_to_strings(accounts)
            return [TextContent(type="text", text=json.dumps(accounts, indent=2))]
        
        elif name == "get_transactions":
            filters = build_transaction_filters(arguments)
            
            transactions = await mm_client.get_transactions(
                limit=arguments.get("limit", 100),
                offset=arguments.get("offset", 0),
                **filters
            )

            transactions = apply_transaction_post_filters(
                transactions,
                merchant_id=arguments.get("merchant_id"),
                amount_min=arguments.get("amount_min"),
                amount_max=arguments.get("amount_max"),
            )

            include_transaction_rules = bool(arguments.get("include_transaction_rules", False))
            transactions = maybe_strip_transaction_rules(transactions, include_transaction_rules)

            # Convert date objects to strings before serialization
            transactions = convert_dates_to_strings(transactions)
            return [TextContent(type="text", text=json.dumps(transactions, indent=2))]

        elif name == "search_transactions":
            filters = build_transaction_filters(arguments)

            transactions = await mm_client.get_transactions(
                limit=arguments.get("limit", 25),
                offset=arguments.get("offset", 0),
                **filters,
            )

            transactions = apply_transaction_post_filters(
                transactions,
                merchant_id=arguments.get("merchant_id"),
                amount_min=arguments.get("amount_min"),
                amount_max=arguments.get("amount_max"),
            )

            include_transaction_rules = bool(arguments.get("include_transaction_rules", False))
            transactions = maybe_strip_transaction_rules(transactions, include_transaction_rules)

            response: Dict[str, Any] = build_compact_transaction_results(transactions)
            if bool(arguments.get("include_raw", False)):
                response["raw"] = transactions

            response = convert_dates_to_strings(response)
            return [TextContent(type="text", text=json.dumps(response, indent=2))]
        
        elif name == "get_budgets":
            kwargs = {}
            if "start_date" in arguments:
                kwargs["start_date"] = datetime.strptime(
                    arguments["start_date"], "%Y-%m-%d"
                ).date().isoformat()
            if "end_date" in arguments:
                kwargs["end_date"] = datetime.strptime(
                    arguments["end_date"], "%Y-%m-%d"
                ).date().isoformat()
            
            try:
                budgets = await get_budgets_lite(
                    kwargs.get("start_date"),
                    kwargs.get("end_date"),
                )
                budgets = convert_dates_to_strings(budgets)
                return [TextContent(type="text", text=json.dumps(budgets, indent=2))]
            except Exception as lite_error:
                # Fall back to upstream helper in case Monarch schema changes again.
                try:
                    budgets = await mm_client.get_budgets(**kwargs)
                    budgets = convert_dates_to_strings(budgets)
                    return [TextContent(type="text", text=json.dumps(budgets, indent=2))]
                except Exception as upstream_error:
                    if (
                        "Something went wrong while processing: None" in str(lite_error)
                        and "Something went wrong while processing: None" in str(upstream_error)
                    ):
                        return [TextContent(type="text", text=json.dumps({
                            "budgets": [],
                            "message": "No budgets configured in your Monarch Money account"
                        }, indent=2))]
                    raise upstream_error
        
        elif name == "get_cashflow":
            kwargs = {}
            if "start_date" in arguments:
                kwargs["start_date"] = datetime.strptime(
                    arguments["start_date"], "%Y-%m-%d"
                ).date().isoformat()
            if "end_date" in arguments:
                kwargs["end_date"] = datetime.strptime(
                    arguments["end_date"], "%Y-%m-%d"
                ).date().isoformat()
            
            cashflow = await mm_client.get_cashflow(**kwargs)
            # Convert date objects to strings before serialization
            cashflow = convert_dates_to_strings(cashflow)
            return [TextContent(type="text", text=json.dumps(cashflow, indent=2))]
        
        elif name == "get_transaction_categories":
            categories = await mm_client.get_transaction_categories()
            # Convert date objects to strings before serialization
            categories = convert_dates_to_strings(categories)
            return [TextContent(type="text", text=json.dumps(categories, indent=2))]

        elif name == "get_transaction_details":
            details = await mm_client.get_transaction_details(
                transaction_id=arguments["transaction_id"],
                redirect_posted=arguments.get("redirect_posted", True),
            )
            details = convert_dates_to_strings(details)
            return [TextContent(type="text", text=json.dumps(details, indent=2))]

        elif name == "get_transaction_tags":
            tags = await mm_client.get_transaction_tags()
            tags = convert_dates_to_strings(tags)
            return [TextContent(type="text", text=json.dumps(tags, indent=2))]
        
        elif name == "create_transaction":
            # Convert date string to date object
            transaction_date = datetime.strptime(arguments["date"], "%Y-%m-%d").date()
            
            result = await mm_client.create_transaction(
                amount=arguments["amount"],
                description=arguments["description"],
                category_id=arguments.get("category_id"),
                account_id=arguments["account_id"],
                date=transaction_date,
                notes=arguments.get("notes")
            )
            # Convert date objects to strings before serialization
            result = convert_dates_to_strings(result)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "update_transaction":
            # Build update parameters
            updates = {"transaction_id": arguments["transaction_id"]}
            if "amount" in arguments:
                updates["amount"] = arguments["amount"]
            if "description" in arguments:
                updates["description"] = arguments["description"]
            if "category_id" in arguments:
                updates["category_id"] = arguments["category_id"]
            if "date" in arguments:
                updates["date"] = datetime.strptime(arguments["date"], "%Y-%m-%d").date()
            if "notes" in arguments:
                updates["notes"] = arguments["notes"]
            
            result = await mm_client.update_transaction(**updates)
            # Convert date objects to strings before serialization
            result = convert_dates_to_strings(result)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        elif name == "refresh_accounts":
            result = await mm_client.request_accounts_refresh()
            # Convert date objects to strings before serialization
            result = convert_dates_to_strings(result)
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        
        else:
            return [TextContent(type="text", text=f"Error: Unknown tool '{name}'")]
    
    except Exception as e:
        if is_auth_error(e) and not internal_retry:
            print(
                f"Detected auth/session error for {name}; re-authenticating and retrying once",
                file=sys.stderr,
            )
            try:
                await refresh_client_session()
                retry_args = dict(arguments)
                retry_args["__auth_retry"] = True
                return await call_tool(name, retry_args)
            except Exception as retry_error:
                return [TextContent(
                    type="text",
                    text=f"Error executing {name} after re-auth attempt: {str(retry_error)}",
                )]

        return [TextContent(type="text", text=f"Error executing {name}: {str(e)}")]


async def main():
    """Main entry point for the server."""
    print(
        f"Using Monarch API base URL: {MonarchMoneyEndpoints.BASE_URL}",
        file=sys.stderr,
    )

    # Run the MCP server
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, 
            write_stream,
            InitializationOptions(
                server_name="monarch-money",
                server_version="1.0.0",
                capabilities=ServerCapabilities(
                    tools={}
                )
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
