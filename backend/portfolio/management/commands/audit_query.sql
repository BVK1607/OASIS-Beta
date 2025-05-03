-- Check for NULL portfolios in market prices
SELECT COUNT(*) as null_portfolios, ticker
FROM portfolio_marketprice
WHERE portfolio_id IS NULL
GROUP BY ticker;

-- Check for orphaned market prices (referencing non-existent portfolios)
SELECT mp.id, mp.ticker, mp.date
FROM portfolio_marketprice mp
LEFT JOIN portfolio p ON mp.portfolio_id = p.id
WHERE mp.portfolio_id IS NOT NULL AND p.id IS NULL;

-- Check for duplicate market prices
SELECT ticker, date, COUNT(*) as count
FROM portfolio_marketprice
GROUP BY ticker, date
HAVING COUNT(*) > 1;

-- Check for holdings without market prices
SELECT h.ticker
FROM holding h
LEFT JOIN portfolio_marketprice mp ON h.ticker = mp.ticker
WHERE mp.id IS NULL AND h.asset_class != 'Cash';

-- Check for market prices without holdings
SELECT DISTINCT mp.ticker
FROM portfolio_marketprice mp
LEFT JOIN holding h ON mp.ticker = h.ticker
WHERE h.id IS NULL;

-- Check for inconsistent portfolio references
SELECT hr.id, hr.portfolio_id as hr_portfolio, h.portfolio_id as holding_portfolio
FROM holding_returns hr
JOIN holding h ON hr.holding_id = h.id
WHERE hr.portfolio_id != h.portfolio_id;

-- Check for invalid values
SELECT 'market_prices' as table_name, COUNT(*) as invalid_count
FROM portfolio_marketprice
WHERE price < 0 OR price IS NULL
UNION ALL
SELECT 'holdings' as table_name, COUNT(*)
FROM holding
WHERE quantity < 0 OR book_cost < 0 OR market_value < 0;

-- Check for future dates
SELECT 'market_prices' as table_name, COUNT(*) as future_dates
FROM portfolio_marketprice
WHERE date > CURRENT_DATE
UNION ALL
SELECT 'holding_returns', COUNT(*)
FROM holding_returns
WHERE date > CURRENT_DATE
UNION ALL
SELECT 'portfolio_nav', COUNT(*)
FROM portfolio_nav
WHERE date > CURRENT_DATE; 