# Monarch Money MCP Server

An MCP (Model Context Protocol) server that provides access to Monarch Money financial data and operations.

## Features

- **Account Management**: List and retrieve account information
- **Transaction Operations**: Get transactions with filtering by date range, text search, accounts, and categories
- **Budget Analysis**: Access budget data and spending insights
- **Category Management**: List and manage transaction categories
- **Goal Tracking**: Access financial goals and progress
- **Net Worth Tracking**: Retrieve net worth snapshots over time

## Installation

1. Clone or download this MCP server
2. Install dependencies:
   ```bash
   cd /path/to/monarch-money-mcp
   uv sync
   ```

## Configuration

Add the server to your `.mcp.json` configuration file:

```json
{
  "mcpServers": {
    "monarch-money": {
      "command": "/path/to/uv",
      "args": [
        "--directory", 
        "/path/to/monarch-money-mcp",
        "run",
        "python",
        "server.py"
      ],
      "env": {
        "MONARCH_EMAIL": "your-email@example.com",
        "MONARCH_PASSWORD": "your-password",
        "MONARCH_MFA_SECRET": "your-mfa-secret-key"
      }
    }
  }
}
```

**Important Notes:**
- Replace `/path/to/uv` with the full path to your `uv` executable (find it with `which uv`)
- Replace `/path/to/monarch-money-mcp` with the absolute path to this server directory
- Use absolute paths, not relative paths

### Getting Your MFA Secret

1. Go to Monarch Money settings and enable 2FA
2. When shown the QR code, look for the "Can't scan?" or "Enter manually" option
3. Copy the secret key (it will be a string like `T5SPVJIBRNPNNINFSH5W7RFVF2XYADYX`)
4. Use this as your `MONARCH_MFA_SECRET`

## Available Tools

### `get_accounts`
List all accounts with their balances and details.

### `get_transactions`
Get transactions with optional filtering:
- `start_date`: Filter transactions from this date (YYYY-MM-DD)
- `end_date`: Filter transactions to this date (YYYY-MM-DD)
- `search`: Filter by merchant/transaction text (for example, `youtube`)
- `account_id`: Filter by one account ID (backwards-compatible)
- `account_ids`: Filter by multiple account IDs
- `category_id`: Filter by one category ID (backwards-compatible)
- `category_ids`: Filter by multiple category IDs
- `tag_ids`: Filter by one or more transaction tag IDs
- `has_attachments`: Filter by attachment presence
- `has_notes`: Filter by notes presence
- `hidden_from_reports`: Filter by hide-from-reports status
- `is_split`: Filter by split transaction status
- `is_recurring`: Filter by recurring transaction status
- `imported_from_mint`: Filter by imported-from-Mint status
- `synced_from_institution`: Filter by institution-sync status
- `merchant_id`: Post-fetch filter for exact `merchant.id`
- `amount_min`: Post-fetch filter for minimum transaction amount (signed amount)
- `amount_max`: Post-fetch filter for maximum transaction amount (signed amount)
- `include_transaction_rules`: Include heavy `transactionRules` array (defaults to `false` to keep responses smaller)
- `limit`: Maximum number of transactions to return

### `get_categories`
List all transaction categories.

### `get_budgets`
Get budget information and spending analysis.

### `get_goals`
List financial goals and their progress.

### `get_cashflow`
Get cashflow data for income and expense analysis.

### `get_investments`
Get investment account details and performance.

### `get_net_worth`
Get net worth snapshots over time.

## Usage Examples

### Basic Account Information
```
Use the get_accounts tool to see all my accounts and their current balances.
```

### Transaction Analysis
```
Get all transactions from January 2024 using get_transactions with start_date "2024-01-01" and end_date "2024-01-31".
```

### Transaction Search (Low-Volume)
```
Use get_transactions with search "youtube", start_date "2024-01-01", end_date "2024-12-31", and limit 25.
```

### Include Raw Transaction Rules (Optional)
```
Use get_transactions with search "youtube", limit 25, and include_transaction_rules true.
```

### Singular and Plural ID Filters
```
Use get_transactions with account_id "acc_123" and category_ids ["cat_1", "cat_2"].
```

### Boolean and Tag Filters
```
Use get_transactions with is_recurring true, has_notes false, and tag_ids ["tag_1", "tag_2"].
```

### Post-Fetch Filters
```
Use get_transactions with search "youtube", merchant_id "merchant_123", amount_min -100, and amount_max -20.
```

### Budget Tracking
```
Show me my current budget status using the get_budgets tool.
```

## Session Management

The server automatically manages authentication sessions:
- Sessions are cached in a `.mm` directory for faster subsequent logins
- The session cache is automatically created and managed
- Use `MONARCH_FORCE_LOGIN=true` in the env section to force a fresh login if needed

## Troubleshooting

### MFA Issues
- Ensure your MFA secret is correct and properly formatted
- Try setting `MONARCH_FORCE_LOGIN=true` in your `.mcp.json` env section
- Check that your system time is accurate (required for TOTP)

### Connection Issues
- Verify your email and password are correct in `.mcp.json`
- Check your internet connection
- Try running the server directly to see detailed error messages:
  ```bash
  uv run server.py
  ```

### Session Problems
- Delete the `.mm` directory to clear cached sessions
- Set `MONARCH_FORCE_LOGIN=true` in your `.mcp.json` env section temporarily

## Credits

### MCP Server
- **Author**: Taurus Colvin ([@colvint](https://github.com/colvint))
- **Description**: MCP (Model Context Protocol) server wrapper for Monarch Money

### MonarchMoney Python Library
- **Author**: hammem ([@hammem](https://github.com/hammem))
- **Repository**: [https://github.com/hammem/monarchmoney](https://github.com/hammem/monarchmoney)
- **License**: MIT License
- **Description**: The underlying Python library that provides API access to Monarch Money

This MCP server wraps the monarchmoney Python library to provide seamless integration with AI assistants through the Model Context Protocol.

## Security Notes

- Keep your credentials secure in your `.mcp.json` file
- The MFA secret provides full access to your account - treat it like a password
- Session files in `.mm` directory contain authentication tokens - keep them secure
- Consider restricting access to your `.mcp.json` file since it contains sensitive credentials
