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
            description="Retrieve all linked financial accounts",
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False
            }
        ),
        Tool(
            name="get_transactions",
            description="Fetch transactions with optional filtering",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of transactions to return",
                        "default": 100
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of transactions to skip",
                        "default": 0
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Start date in YYYY-MM-DD format"
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date in YYYY-MM-DD format"
                    },
                    "search": {
                        "type": "string",
                        "description": "Filter by merchant/transaction text (for example, 'YouTube TV')"
                    },
                    "account_id": {
                        "type": "string",
                        "description": "Filter by specific account ID"
                    },
                    "account_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by one or more account IDs"
                    },
                    "category_id": {
                        "type": "string",
                        "description": "Filter by specific category ID"
                    },
                    "category_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by one or more category IDs"
                    },
                    "include_transaction_rules": {
                        "type": "boolean",
                        "description": "Include transactionRules in response payload (default false to reduce output size)",
                        "default": False
                    }
                },
                "additionalProperties": False
            }
        ),
        Tool(
            name="get_budgets",
            description="Retrieve budget information",
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
            description="Analyze cashflow data",
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
            description="List all transaction categories",
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False
            }
        ),
        Tool(
            name="create_transaction",
            description="Create a new transaction",
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
            description="Update an existing transaction",
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
            description="Request a refresh of all account data from financial institutions",
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
            # Build filter parameters
            filters = {}
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
            
            transactions = await mm_client.get_transactions(
                limit=arguments.get("limit", 100),
                offset=arguments.get("offset", 0),
                **filters
            )

            # Reduce payload size by default; transactionRules can be very large and
            # usually aren't needed for analysis prompts.
            include_transaction_rules = bool(arguments.get("include_transaction_rules", False))
            if (
                not include_transaction_rules
                and isinstance(transactions, dict)
                and isinstance(transactions.get("transactionRules"), list)
            ):
                transactions = dict(transactions)
                transactions.pop("transactionRules", None)

            # Convert date objects to strings before serialization
            transactions = convert_dates_to_strings(transactions)
            return [TextContent(type="text", text=json.dumps(transactions, indent=2))]
        
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
