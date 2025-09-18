from alembic import op
import sqlalchemy as sa

revision = '0001_init'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('users',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tg_id', sa.BigInteger(), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False, server_default='user'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.Column('last_login_at', sa.DateTime(timezone=True)),
        sa.Column('settings', sa.JSON(), server_default='{}')
    )
    op.create_index('ix_users_tg_id', 'users', ['tg_id'], unique=True)

    op.create_table('user_credentials',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('key_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('wb_api_key_encrypted', sa.String(), nullable=False),
        sa.Column('salt', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), onupdate=sa.text('now()'))
    )
    op.create_index('ix_user_credentials_user_id', 'user_credentials', ['user_id'])

    op.create_table('products',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('nm_id', sa.BigInteger(), nullable=False),
        sa.Column('sku', sa.String()),
        sa.Column('title', sa.String()),
        sa.Column('brand', sa.String()),
        sa.Column('category', sa.String()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'))
    )
    op.create_index('ix_products_user_id', 'products', ['user_id'])
    op.create_index('ix_products_nm_id', 'products', ['nm_id'])

    op.create_table('sales',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('nm_id', sa.BigInteger(), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('qty', sa.Integer(), server_default='0'),
        sa.Column('revenue', sa.Numeric(14,2), server_default='0'),
        sa.Column('refund_qty', sa.Integer(), server_default='0'),
        sa.Column('margin', sa.Numeric(14,2))
    )
    op.create_index('ix_sales_user_date', 'sales', ['user_id','date'])
    op.create_index('ix_sales_user_nm_date', 'sales', ['user_id','nm_id','date'])

    op.create_table('stocks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('nm_id', sa.BigInteger(), nullable=False),
        sa.Column('warehouse', sa.String()),
        sa.Column('region', sa.String()),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('qty', sa.Integer(), server_default='0')
    )
    op.create_index('ix_stocks_user_nm', 'stocks', ['user_id','nm_id'])
    op.create_index('ix_stocks_region_wh', 'stocks', ['region','warehouse'])

    op.create_table('orders',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('nm_id', sa.BigInteger(), nullable=False),
        sa.Column('order_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String()),
        sa.Column('qty', sa.Integer(), server_default='1'),
        sa.Column('lead_time_days', sa.Integer())
    )
    op.create_index('ix_orders_user_date', 'orders', ['user_id','order_date'])
    op.create_index('ix_orders_user_status', 'orders', ['user_id','status'])

    op.create_table('supply_plan',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('nm_id', sa.BigInteger(), nullable=False),
        sa.Column('region', sa.String()),
        sa.Column('warehouse', sa.String()),
        sa.Column('horizon_days', sa.Integer(), nullable=False),
        sa.Column('recommended_qty', sa.Integer(), server_default='0'),
        sa.Column('rationale', sa.JSON(), server_default='{}'),
        sa.Column('generated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'))
    )
    op.create_index('ix_supply_user_nm_h', 'supply_plan', ['user_id','nm_id','horizon_days'])

def downgrade():
    op.drop_table('supply_plan')
    op.drop_index('ix_orders_user_status', table_name='orders')
    op.drop_index('ix_orders_user_date', table_name='orders')
    op.drop_table('orders')
    op.drop_index('ix_stocks_region_wh', table_name='stocks')
    op.drop_index('ix_stocks_user_nm', table_name='stocks')
    op.drop_table('stocks')
    op.drop_index('ix_sales_user_nm_date', table_name='sales')
    op.drop_index('ix_sales_user_date', table_name='sales')
    op.drop_table('sales')
    op.drop_index('ix_products_nm_id', table_name='products')
    op.drop_index('ix_products_user_id', table_name='products')
    op.drop_table('products')
    op.drop_index('ix_user_credentials_user_id', table_name='user_credentials')
    op.drop_table('user_credentials')
    op.drop_index('ix_users_tg_id', table_name='users')
    op.drop_table('users')
