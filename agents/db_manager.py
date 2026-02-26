import sqlite3
import os
from dotenv import load_dotenv

class DatabaseManager:
    def __init__(self):
        load_dotenv()
        
        self.db_path = os.getenv("DB_PATH", "./data/magnus_v3.db")
        
        
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._initialize_db()

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize_db(self):
        """Create tables if not exist; run column migrations."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        token_id TEXT NOT NULL,
                        market_id TEXT,
                        question TEXT,
                        category TEXT,
                        buy_price REAL,
                        amount_usdc REAL,
                        shares_bought REAL,
                        status TEXT DEFAULT 'OPEN',
                        selling_in_progress BOOLEAN DEFAULT 0,
                        order_active_in_book BOOLEAN DEFAULT 0,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        notes TEXT
                    )
                """)
                conn.commit()
                # Migration: add columns if missing
                try:
                    cursor.execute("ALTER TABLE trades ADD COLUMN category TEXT")
                    conn.commit()
                except sqlite3.OperationalError:
                    pass
                for col, typ in [("spread_pct", "REAL"), ("target_price", "REAL"), ("end_date_iso", "TEXT"), ("event_id", "TEXT")]:
                    try:
                        cursor.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
                        conn.commit()
                    except sqlite3.OperationalError:
                        pass
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS analyses (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        question TEXT NOT NULL,
                        category TEXT,
                        action TEXT,
                        reason TEXT,
                        max_price REAL,
                        current_price REAL,
                        hype_score INTEGER,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.commit()
        except Exception as e:
            print(f"❌ DB Init Error: {e}")

    def log_new_trade(self, token_id: str, market_id: str, question: str, buy_price: float, amount_usdc: float, shares_bought: float, notes: str = "", category: str = "", spread_pct: float | None = None, target_price: float | None = None, end_date_iso: str | None = None, event_id: str | None = None) -> bool:
        """Log a new trade after buy execution."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO trades (token_id, market_id, question, category, buy_price, amount_usdc, shares_bought, notes, spread_pct, target_price, end_date_iso, event_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (token_id, market_id, question, category or "", buy_price, amount_usdc, shares_bought, notes, spread_pct, target_price, end_date_iso or "", event_id or ""))
                conn.commit()
                return True
        except Exception as e:
            print(f"❌ DB Log Trade Error: {e}")
            return False

    def log_analysis(self, question: str, category: str, action: str, reason: str = "", max_price: float = 0, current_price: float = 0, hype_score: int = 0) -> bool:
        """Log each AI analysis (BUY/REJECT) for history and ChromaDB."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO analyses (question, category, action, reason, max_price, current_price, hype_score)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (question[:2000], (category or "")[:100], action or "REJECT", (reason or "")[:1000], max_price, current_price, hype_score))
                conn.commit()
                return True
        except Exception as e:
            print(f"❌ DB Log Analysis Error: {e}")
            return False

    def get_all_analyses(self, limit: int | None = None) -> list[dict]:
        """All analyses, newest first."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                sql = "SELECT id, question, category, action, reason, max_price, current_price, hype_score, created_at FROM analyses ORDER BY id DESC"
                if limit:
                    sql += f" LIMIT {int(limit)}"
                cursor.execute(sql)
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            print(f"❌ DB Get Analyses Error: {e}")
            return []

    def has_ever_traded_market(self, market_id: str) -> bool:
        """True if we ever held a position in this market (prevents re-buying after sell)."""
        if not market_id:
            return False
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM trades WHERE market_id = ? LIMIT 1", (str(market_id),))
                return cursor.fetchone() is not None
        except Exception:
            return False

    def get_open_positions(self) -> list[dict]:
        """All currently open positions."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT 
                        id, token_id, market_id, question, category,
                        buy_price, amount_usdc, shares_bought, status,
                        selling_in_progress, order_active_in_book, timestamp, notes,
                        spread_pct, target_price, end_date_iso, event_id
                    FROM trades 
                    WHERE status = 'OPEN'
                """)
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            print(f"❌ DB Get Open Positions Error: {e}")
            return []

    def get_all_trades(self, limit: int | None = None) -> list[dict]:
        """All trades (any status), newest first."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                sql = """
                    SELECT id, token_id, market_id, question, category,
                           buy_price, amount_usdc, shares_bought, status,
                           timestamp, notes
                    FROM trades ORDER BY id DESC
                """
                if limit:
                    sql += f" LIMIT {int(limit)}"
                cursor.execute(sql)
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            print(f"❌ DB Get All Trades Error: {e}")
            return []

    def update_trade_status(self, token_id: str, new_status: str, extra_notes: str = "") -> bool:
        """Update trade status (e.g. CLOSED_PROFIT, CLOSED_LOSS)."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                
                if extra_notes:
                    cursor.execute("""
                        UPDATE trades 
                        SET status = ?, 
                            notes = IFNULL(notes, '') || ' | ' || ?
                        WHERE token_id = ?
                    """, (new_status, extra_notes, token_id))
                else:
                    cursor.execute("""
                        UPDATE trades 
                        SET status = ?
                        WHERE token_id = ?
                    """, (new_status, token_id))
                    
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            print(f"❌ DB Update Status Error: {e}")
            return False

    def set_selling_flags(self, token_id: str, in_progress: bool, active_in_book: bool) -> bool:
        """Set flags to prevent duplicate sell attempts."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE trades 
                    SET selling_in_progress = ?, order_active_in_book = ?
                    WHERE token_id = ?
                """, (int(in_progress), int(active_in_book), token_id))
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            print(f"❌ DB Set Flags Error: {e}")
            return False

if __name__ == "__main__":
    db = DatabaseManager()
    print("✅ Database Manager initialized.")