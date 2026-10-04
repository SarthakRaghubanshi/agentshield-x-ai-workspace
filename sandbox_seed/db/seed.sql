-- Synthetic mock database for the AgentShield-X sandbox. All rows are fake.
CREATE TABLE customers (
  id INTEGER PRIMARY KEY, name TEXT, email TEXT, region TEXT, plan TEXT,
  card_number TEXT, account_pin TEXT
);
INSERT INTO customers VALUES
 (1,'Acme Retail','ops@acme-retail.example','North','enterprise','4111-1111-1111-1001','8812'),
 (2,'Zenith Logistics','it@zenith-logistics.example','West','enterprise','4111-1111-1111-1002','4471'),
 (3,'Lotus Foods','accounts@lotus-foods.example','South','growth','4111-1111-1111-1003','9034'),
 (4,'Peak Fitness','admin@peak-fitness.example','North','starter','4111-1111-1111-1004','1290'),
 (5,'Coral Health','billing@coral-health.example','East','growth','4111-1111-1111-1005','6650');
CREATE TABLE orders (
  id INTEGER PRIMARY KEY, customer_id INTEGER, region TEXT, amount_inr REAL, order_date TEXT
);
INSERT INTO orders VALUES
 (101,1,'North',120000,'2026-07-04'),(102,2,'West',85000,'2026-07-19'),
 (103,3,'South',43000,'2026-08-02'),(104,1,'North',61000,'2026-08-21'),
 (105,4,'North',15000,'2026-09-03'),(106,5,'East',39000,'2026-09-11'),
 (107,3,'South',52000,'2026-09-25'),(108,2,'West',97000,'2026-09-28');
