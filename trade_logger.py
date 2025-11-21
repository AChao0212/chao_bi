
import csv
import os
from datetime import datetime, timedelta
from config import TRADE_LOG_CSV_PATH

# The headers for the CSV file.
CSV_HEADERS = [
    "timestamp",       # Timestamp of when the trade was closed
    "symbol",          # Trading pair (e.g., BTCUSDT)
    "position_side",   # Direction of the trade: 'LONG' or 'SHORT'
    "entry_price",     # The price at which the position was opened
    "exit_price",      # The price at which the position was closed
    "quantity",        # The size of the trade
    "leverage",        # The leverage used for the trade
    "pnl",             # Realized profit or loss in USDT
    "signal_source",   # The Telegram channel or group the signal came from
    "win_loss_draw",   # Result of the trade: 'WIN', 'LOSS', or 'DRAW'
    "raw_signal",      # The raw text of the trading signal message
]

def log_trade(trade_details):
    """
    Logs the details of a completed trade to a CSV file.

    Args:
        trade_details (dict): A dictionary containing the trade's information.
                              It should include keys matching the CSV_HEADERS.
    """
    # Check if the CSV file exists. If not, create it and write the headers.
    file_exists = os.path.exists(TRADE_LOG_CSV_PATH)
    
    with open(TRADE_LOG_CSV_PATH, mode='a', newline='', encoding='utf-8') as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_HEADERS)
        
        # If the file is new, write the header row first.
        if not file_exists:
            writer.writeheader()
        
        # Prepare the row data from the trade_details dictionary.
        row_data = {header: trade_details.get(header, "") for header in CSV_HEADERS}
        
        # Set the timestamp for when the trade is being logged.
        row_data["timestamp"] = (datetime.utcnow() + timedelta(hours=8)).isoformat()

        # Write the trade's data to the CSV file.
        writer.writerow(row_data)
        
    print(f"📈 Trade logged to {TRADE_LOG_CSV_PATH}: Symbol={row_data['symbol']}, PnL={row_data['pnl']}")

def get_trade_statistics():
    """
    Reads the trade log and calculates trade statistics.

    Returns:
        dict: A dictionary containing:
            - total_trades (int)
            - winning_trades (int)
            - losing_trades (int)
            - draw_trades (int)
            - win_rate (float)
            - loss_rate (float)
            - draw_rate (float)
            - total_pnl (float)
    """
    if not os.path.exists(TRADE_LOG_CSV_PATH):
        return {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "draw_trades": 0,
            "win_rate": 0.0,
            "loss_rate": 0.0,
            "draw_rate": 0.0,
            "total_pnl": 0.0,
        }

    total_trades = 0
    winning_trades = 0
    losing_trades = 0
    draw_trades = 0
    total_pnl = 0.0

    with open(TRADE_LOG_CSV_PATH, mode='r', newline='', encoding='utf-8') as csv_file:
        reader = csv.DictReader(csv_file)
        for row in reader:
            total_trades += 1
            try:
                pnl = float(row.get('pnl', 0.0))
                total_pnl += pnl
                if pnl > 0:
                    winning_trades += 1
                elif pnl < 0:
                    losing_trades += 1
                else:
                    draw_trades += 1
            except ValueError:
                # Handle cases where pnl might not be a valid float
                pass

    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0
    loss_rate = (losing_trades / total_trades * 100) if total_trades > 0 else 0.0
    draw_rate = (draw_trades / total_trades * 100) if total_trades > 0 else 0.0

    return {
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "draw_trades": draw_trades,
        "win_rate": round(win_rate, 2),
        "loss_rate": round(loss_rate, 2),
        "draw_rate": round(draw_rate, 2),
        "total_pnl": round(total_pnl, 2),
    }

