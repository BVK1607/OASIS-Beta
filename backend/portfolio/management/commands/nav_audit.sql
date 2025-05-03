-- Check for holdings without recent market prices (last 7 days)
WITH recent_dates AS (
    SELECT date FROM portfolio_marketprice
    WHERE date >= date('now', '-7 days')
    GROUP BY date
)
SELECT h.ticker, h.portfolio_id, COUNT(mp.id) as price_count
FROM holding h
LEFT JOIN portfolio_marketprice mp ON h.ticker = mp.ticker
AND mp.date IN (SELECT date FROM recent_dates)
WHERE h.asset_class != 'Cash'
GROUP BY h.ticker, h.portfolio_id
HAVING price_count = 0;

-- Check for holdings without holding returns
SELECT h.ticker, h.portfolio_id, COUNT(hr.id) as return_count
FROM holding h
LEFT JOIN holding_returns hr ON h.id = hr.holding_id
WHERE h.asset_class != 'Cash'
GROUP BY h.ticker, h.portfolio_id
HAVING return_count = 0;

-- Check for portfolios without NAV records
SELECT p.id, p.name, COUNT(nav.id) as nav_count
FROM portfolio p
LEFT JOIN portfolio_nav nav ON p.id = nav.portfolio_id
GROUP BY p.id, p.name
HAVING nav_count = 0;

-- Check for large NAV changes (>10% daily change)
WITH nav_changes AS (
    SELECT 
        portfolio_id,
        date,
        total_value,
        LAG(total_value) OVER (PARTITION BY portfolio_id ORDER BY date) as prev_value,
        LAG(date) OVER (PARTITION BY portfolio_id ORDER BY date) as prev_date
    FROM portfolio_nav
)
SELECT 
    n.*,
    p.name as portfolio_name,
    ROUND(((total_value - prev_value) * 100.0 / prev_value), 2) as pct_change
FROM nav_changes n
JOIN portfolio p ON n.portfolio_id = p.id
WHERE 
    prev_value IS NOT NULL 
    AND ABS((total_value - prev_value) * 100.0 / prev_value) > 10;

-- Check for mismatches between holding market values and latest market prices
SELECT 
    h.ticker,
    h.portfolio_id,
    h.market_value as holding_market_value,
    (h.quantity * mp.price) as calculated_market_value,
    ABS(h.market_value - (h.quantity * mp.price)) as difference
FROM holding h
JOIN (
    SELECT ticker, price, date,
    ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) as rn
    FROM portfolio_marketprice
) mp ON h.ticker = mp.ticker AND mp.rn = 1
WHERE 
    h.asset_class != 'Cash'
    AND ABS(h.market_value - (h.quantity * mp.price)) > 0.01;

-- Check for gaps in NAV dates (excluding weekends)
WITH RECURSIVE dates(date) AS (
    SELECT MIN(date) FROM portfolio_nav
    UNION ALL
    SELECT date(date, '+1 day')
    FROM dates
    WHERE date < (SELECT MAX(date) FROM portfolio_nav)
),
trading_days AS (
    SELECT date 
    FROM dates 
    WHERE strftime('%w', date) NOT IN ('0', '6')  -- Exclude weekends
)
SELECT p.id, p.name, d.date as missing_date
FROM portfolio p
CROSS JOIN trading_days d
LEFT JOIN portfolio_nav n ON p.id = n.portfolio_id AND d.date = n.date
WHERE n.id IS NULL
ORDER BY p.id, d.date; 