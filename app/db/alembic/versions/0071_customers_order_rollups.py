"""add customer rollups for marketplace orders

Revision ID: 0071_customers
Revises: 0070_business_agents
Create Date: 2026-06-02
"""

from alembic import op
import sqlalchemy as sa


revision = "0071_customers"
down_revision = "0070_business_agents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("marketplace", sa.String(length=64), nullable=False, server_default="ebay"),
        sa.Column("customer_key", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("ebay_username", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("display_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("primary_email", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("shipping_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("shipping_address_line1", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("shipping_address_line2", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("shipping_city", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("shipping_state", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("shipping_postal_code", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("shipping_country", sa.String(length=8), nullable=False, server_default=""),
        sa.Column("first_order_at", sa.DateTime(), nullable=True),
        sa.Column("last_order_at", sa.DateTime(), nullable=True),
        sa.Column("order_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_spend", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("is_repeat_buyer", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("marketplace", "customer_key", name="uq_customer_marketplace_key"),
    )
    op.create_index("ix_customers_marketplace", "customers", ["marketplace"], unique=False)
    op.create_index("ix_customers_customer_key", "customers", ["customer_key"], unique=False)
    op.create_index("ix_customers_marketplace_username", "customers", ["marketplace", "ebay_username"], unique=False)
    op.create_index("ix_customers_email", "customers", ["primary_email"], unique=False)
    op.create_index("ix_customers_first_order_at", "customers", ["first_order_at"], unique=False)
    op.create_index("ix_customers_last_order_at", "customers", ["last_order_at"], unique=False)
    op.create_index("ix_customers_order_count", "customers", ["order_count"], unique=False)
    op.create_index("ix_customers_is_repeat_buyer", "customers", ["is_repeat_buyer"], unique=False)

    with op.batch_alter_table("orders") as batch_op:
        batch_op.add_column(sa.Column("customer_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_orders_customer_id_customers",
            "customers",
            ["customer_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_orders_customer_id", "orders", ["customer_id"], unique=False)

    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            INSERT INTO customers (
                marketplace,
                customer_key,
                ebay_username,
                display_name,
                primary_email,
                shipping_name,
                shipping_city,
                shipping_state,
                shipping_postal_code,
                shipping_country,
                first_order_at,
                last_order_at,
                order_count,
                total_spend,
                is_repeat_buyer
            )
            SELECT
                marketplace,
                customer_key,
                max(buyer_username),
                max(buyer_name),
                max(lower(buyer_email)),
                max(buyer_name),
                max(ship_to_city),
                max(ship_to_state),
                max(ship_to_postal_code),
                max(ship_to_country),
                min(sold_at),
                max(sold_at),
                count(*),
                coalesce(sum(total_amount), 0),
                count(*) > 1
            FROM (
                SELECT
                    marketplace,
                    buyer_username,
                    buyer_name,
                    buyer_email,
                    ship_to_city,
                    ship_to_state,
                    ship_to_postal_code,
                    ship_to_country,
                    sold_at,
                    total_amount,
                    CASE
                        WHEN coalesce(trim(buyer_username), '') <> ''
                            THEN 'username:' || lower(trim(buyer_username))
                        WHEN coalesce(trim(buyer_email), '') <> ''
                            THEN 'email:' || lower(trim(buyer_email))
                        WHEN coalesce(trim(buyer_name), '') <> '' OR coalesce(trim(ship_to_postal_code), '') <> ''
                            THEN 'ship:' || lower(trim(coalesce(buyer_name, ''))) || ':' || lower(trim(coalesce(ship_to_postal_code, '')))
                        ELSE ''
                    END AS customer_key
                FROM orders
            ) keyed_orders
            WHERE customer_key <> ''
            GROUP BY marketplace, customer_key
            """
        )
    )
    conn.execute(
        sa.text(
            """
            UPDATE orders
            SET customer_id = customers.id
            FROM customers
            WHERE orders.marketplace = customers.marketplace
              AND customers.customer_key = CASE
                    WHEN coalesce(trim(orders.buyer_username), '') <> ''
                        THEN 'username:' || lower(trim(orders.buyer_username))
                    WHEN coalesce(trim(orders.buyer_email), '') <> ''
                        THEN 'email:' || lower(trim(orders.buyer_email))
                    WHEN coalesce(trim(orders.buyer_name), '') <> '' OR coalesce(trim(orders.ship_to_postal_code), '') <> ''
                        THEN 'ship:' || lower(trim(coalesce(orders.buyer_name, ''))) || ':' || lower(trim(coalesce(orders.ship_to_postal_code, '')))
                    ELSE ''
                  END
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_orders_customer_id", table_name="orders")
    with op.batch_alter_table("orders") as batch_op:
        batch_op.drop_constraint("fk_orders_customer_id_customers", type_="foreignkey")
        batch_op.drop_column("customer_id")
    op.drop_index("ix_customers_is_repeat_buyer", table_name="customers")
    op.drop_index("ix_customers_order_count", table_name="customers")
    op.drop_index("ix_customers_last_order_at", table_name="customers")
    op.drop_index("ix_customers_first_order_at", table_name="customers")
    op.drop_index("ix_customers_email", table_name="customers")
    op.drop_index("ix_customers_marketplace_username", table_name="customers")
    op.drop_index("ix_customers_customer_key", table_name="customers")
    op.drop_index("ix_customers_marketplace", table_name="customers")
    op.drop_table("customers")
