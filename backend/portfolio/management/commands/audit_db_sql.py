from django.core.management.base import BaseCommand
from django.db import connection

class Command(BaseCommand):
    help = 'Audit database using SQL queries'

    def handle(self, *args, **options):
        self.stdout.write('Starting SQL database audit...\n')

        with connection.cursor() as cursor:
            # Check for missing foreign key relationships
            self.stdout.write('\n1. Checking foreign key relationships...')
            
            # MarketPrice -> Portfolio
            cursor.execute("""
                SELECT COUNT(*) 
                FROM portfolio_marketprice mp
                LEFT JOIN portfolio p ON mp.portfolio_id = p.id
                WHERE mp.portfolio_id IS NOT NULL 
                AND p.id IS NULL
            """)
            invalid_portfolio_refs = cursor.fetchone()[0]
            self.stdout.write(f'- Found {invalid_portfolio_refs} market prices referencing non-existent portfolios')

            # HoldingReturn -> Portfolio
            cursor.execute("""
                SELECT COUNT(*) 
                FROM holding_returns hr
                LEFT JOIN portfolio p ON hr.portfolio_id = p.id
                WHERE hr.portfolio_id IS NOT NULL 
                AND p.id IS NULL
            """)
            invalid_hr_portfolio_refs = cursor.fetchone()[0]
            self.stdout.write(f'- Found {invalid_hr_portfolio_refs} holding returns referencing non-existent portfolios')

            # HoldingReturn -> Holding
            cursor.execute("""
                SELECT COUNT(*) 
                FROM holding_returns hr
                LEFT JOIN holding h ON hr.holding_id = h.id
                WHERE hr.holding_id IS NOT NULL 
                AND h.id IS NULL
            """)
            invalid_hr_holding_refs = cursor.fetchone()[0]
            self.stdout.write(f'- Found {invalid_hr_holding_refs} holding returns referencing non-existent holdings')

            # Check for duplicate primary keys
            self.stdout.write('\n2. Checking for duplicate primary keys...')
            tables = ['portfolio', 'holding', 'portfolio_marketprice', 'holding_returns', 'portfolio_nav']
            
            for table in tables:
                cursor.execute(f"""
                    SELECT id, COUNT(*)
                    FROM {table}
                    GROUP BY id
                    HAVING COUNT(*) > 1
                """)
                duplicates = cursor.fetchall()
                if duplicates:
                    self.stdout.write(f'- Found {len(duplicates)} duplicate IDs in {table}')
                    for dup in duplicates:
                        self.stdout.write(f'  * ID {dup[0]} appears {dup[1]} times')

            # Check for inconsistent portfolio references
            self.stdout.write('\n3. Checking for inconsistent portfolio references...')
            cursor.execute("""
                SELECT COUNT(*)
                FROM holding_returns hr
                JOIN holding h ON hr.holding_id = h.id
                WHERE hr.portfolio_id != h.portfolio_id
            """)
            inconsistent_portfolios = cursor.fetchone()[0]
            self.stdout.write(f'- Found {inconsistent_portfolios} holding returns with mismatched portfolio references')

            # Check for orphaned records
            self.stdout.write('\n4. Checking for orphaned records...')
            cursor.execute("""
                SELECT 
                    (SELECT COUNT(*) FROM portfolio_marketprice WHERE portfolio_id IS NULL) as orphaned_prices,
                    (SELECT COUNT(*) FROM holding_returns WHERE portfolio_id IS NULL) as orphaned_returns,
                    (SELECT COUNT(*) FROM portfolio_nav WHERE portfolio_id IS NULL) as orphaned_navs
            """)
            orphaned = cursor.fetchone()
            self.stdout.write(f'- Found {orphaned[0]} orphaned market prices')
            self.stdout.write(f'- Found {orphaned[1]} orphaned holding returns')
            self.stdout.write(f'- Found {orphaned[2]} orphaned NAVs')

            # Check for data type consistency
            self.stdout.write('\n5. Checking for data type consistency...')
            cursor.execute("""
                SELECT COUNT(*)
                FROM portfolio_marketprice
                WHERE price < 0 OR price IS NULL
            """)
            invalid_prices = cursor.fetchone()[0]
            self.stdout.write(f'- Found {invalid_prices} market prices with invalid values')

            cursor.execute("""
                SELECT COUNT(*)
                FROM holding
                WHERE quantity < 0 OR book_cost < 0 OR market_value < 0
            """)
            invalid_holdings = cursor.fetchone()[0]
            self.stdout.write(f'- Found {invalid_holdings} holdings with invalid values')

            # Check for date consistency
            self.stdout.write('\n6. Checking for date consistency...')
            cursor.execute("""
                SELECT 
                    (SELECT COUNT(*) FROM portfolio_marketprice WHERE date > CURRENT_DATE) as future_prices,
                    (SELECT COUNT(*) FROM holding_returns WHERE date > CURRENT_DATE) as future_returns,
                    (SELECT COUNT(*) FROM portfolio_nav WHERE date > CURRENT_DATE) as future_navs
            """)
            future_dates = cursor.fetchone()
            self.stdout.write(f'- Found {future_dates[0]} market prices with future dates')
            self.stdout.write(f'- Found {future_dates[1]} holding returns with future dates')
            self.stdout.write(f'- Found {future_dates[2]} NAV records with future dates')

        self.stdout.write('\nSQL database audit complete.') 