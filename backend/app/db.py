from sqlalchemy import Engine, create_engine


def create_database_engine(database_url: str) -> Engine:
    return create_engine(
        database_url,
        connect_args={"connect_timeout": 2},
        pool_pre_ping=True,
    )
