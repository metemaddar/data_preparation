from src.config.config import Config
from src.core.config import settings
from src.db.db import Database
from src.collection.kart.prepare_kart import PrepareKart
from uuid import uuid4
from src.utils.utils import print_info


class Subscription:
    """Class to prepare the POIs from OpenStreetMap."""

    def __init__(self, db: Database):
        """Constructor method.

        Args:
            db (Database): Database object
        """
        self.db = db
        self.db_config = self.db.db_config
        self.db_uri = f"postgresql://{self.db_config.user}:{self.db_config.password}@{self.db_config.host}:{self.db_config.port}{self.db_config.path}"
        self.engine = self.db.return_sqlalchemy_engine()
        self.root_dir = "/app"

        self.table_name = "poi"
        self.config_pois = Config(self.table_name)
        self.config_pois_preparation = self.config_pois.preparation
        self.repo_url = self.config_pois.subscription["repo_url"]
        self.batch_size = 10000
        self.max_commit_size = 100000

        self.prepare_kart = PrepareKart(
            self.db,
            repo_url=self.repo_url,
            maintainer=self.db_config.user,
            table_name=self.table_name,
        )

    def read_poi(self, category: str):
        """Method to read the relevant POIs from one category into a temporary table based on the subscription criteria.

        Args:
            category (str): Category of POIs to read
        """

        # Get the geometry filter by checking subscriptions
        sql_geom_filter = f"""
            DROP TABLE IF EXISTS geom_filter;
            CREATE TABLE geom_filter AS 
            WITH geom_to_subscribe AS 
            (
                SELECT ST_UNION(n.geom) AS geom 
                FROM kart_poi_goat.data_subscription d, kart_poi_goat.nuts n 
                WHERE d.category = '{category}'
                AND d.nuts_id = n.nuts_id 
                AND d.rule = 'subscribe'
            ),
            geom_to_unsubscribe AS 
            (
                SELECT ST_UNION(n.geom) AS geom 
                FROM kart_poi_goat.data_subscription d, kart_poi_goat.nuts n 
                WHERE d.category = '{category}'
                AND d.nuts_id = n.nuts_id 
                AND d.rule = 'unsubscribe'
            )
            SELECT CASE WHEN s.geom IS NULL AND u.geom IS NOT NULL THEN u.geom 
            WHEN s.geom IS NOT NULL AND u.geom IS NULL THEN s.geom 
            WHEN s.geom IS NULL AND u.geom IS NULL THEN NULL  
            ELSE ST_DIFFERENCE(s.geom, u.geom) END AS geom 
            FROM geom_to_subscribe s, geom_to_unsubscribe u;
        """
        self.db.perform(sql_geom_filter)

        # Get the pois to integrate and check if they fit the geometry filter and validation rules
        sql_create_poi_to_integrate = f"""
            DROP TABLE IF EXISTS poi_to_seed; 
            CREATE TABLE poi_to_seed AS 
            SELECT osm_id, osm_type, category, name, OPERATOR, 
            street, housenumber, zipcode, phone, 
            CASE WHEN (octet_length(tags ->> 'email') BETWEEN 6 AND 320 AND tags ->> 'email' LIKE '_%@_%.__%')
            THEN tags ->> 'email' ELSE NULL END AS email,
            CASE WHEN website ~* '^[a-z](?:[-a-z0-9\+\.])*:(?:\/\/(?:(?:%[0-9a-f][0-9a-f]|[-a-z0-9\._~!\$&''\(\)\*\+,;=:@])|[\/\?])*)?' :: TEXT
            THEN website ELSE NULL END AS website, 
            CASE WHEN (tags->> 'capacity') ~ '^[0-9\.]+$' 
            THEN (tags->> 'capacity')::integer ELSE NULL END AS capacity, 
            opening_hours, 
            CASE WHEN (tags->> 'wheelchair') IN ('yes', 'no', 'limited')
            THEN (tags->> 'wheelchair') ELSE NULL 
            END AS wheelchair, 
            (jsonb_strip_nulls(
                jsonb_build_object(
                    'origin', origin, 'organic', organic, 'subway', subway, 'amenity', amenity, 
                    'shop', shop, 'tourism', tourism, 'railway', railway, 'leisure', leisure, 'sport', sport, 'highway',
                    highway, 'public_transport', public_transport, 'brand', brand
                ) || tags 
            ) - 'email' - 'wheelchair' - 'capacity')::text AS tags, p.geom
            FROM temporal.poi_osm p, geom_filter f 
            WHERE ST_Intersects(p.geom, f.geom)
            AND p.category = '{category}'; 
            CREATE INDEX ON poi_to_seed (osm_id, osm_type);
        """
        self.db.perform(sql_create_poi_to_integrate)

    def get_row_count(self, category: str):
        """Counts the number of rows from the temporary table.

        Args:
            category (str): Category of POIs to read

        Returns:
            row_cnt (int): Number of rows to process
        """
        # Get the number of rows to insert and update
        sql_row_count = f"""
            SELECT COUNT(*) FROM poi_to_seed;
        """
        row_cnt = self.db.select(sql_row_count)
        print_info(
            f"Number of rows to process for category {category}: {row_cnt[0][0]}"
        )
        return row_cnt[0][0]

    def insert_poi(self, row_cnt: int, category: str):
        """Inserts the POIs into Kart POI table in case the combination of osm_id and osm_type is not already present.

        Args:
            row_cnt (int): Number of rows to process
            category (str): Category of POIs to read
        """

        # Read rows in batches and insert them into Kart POI table
        for i in range(0, row_cnt, self.batch_size):
            # Insert the POIs if combination of OSM_ID and OSM_TYPE are not already in the database
            sql_insert_poi = f"""
                INSERT INTO kart_poi_goat.poi(osm_id, osm_type, category, name, OPERATOR,
                street, housenumber, zipcode, phone, email, website, capacity, opening_hours, 
                wheelchair, tags, geom, SOURCE) 
                SELECT p.*, 'OSM'
                FROM (
                    SELECT * 
                    FROM poi_to_seed n 
                    ORDER BY osm_id 
                    LIMIT {self.batch_size} 
                    OFFSET {i}
                ) p
                LEFT JOIN kart_poi_goat.poi n
                ON p.osm_id = n.osm_id
                AND p.osm_type = n.osm_type
                WHERE n.osm_id IS NULL; 
            """
            self.db.perform(sql_insert_poi)

            # Print the number of rows processed
            processed = i + self.batch_size
            if processed > row_cnt:
                processed = row_cnt

            print_info(
                f"Processed INSERT for {processed} rows out of {row_cnt} rows for category {category}"
            )
            # Commit to repository of max_commit_size rows have been reached or when all rows have been processed
            if processed % self.max_commit_size == 0 or processed == row_cnt:
                print_info(f"Commit changes for category {category} started")
                self.prepare_kart.commit(
                    f"Automatically INSERT category {category} with {processed} rows"
                )
                print_info(f"Commit changes for category {category} finished")

    def update_poi(self, row_cnt: int, category: str):
        """Updates the POIs in Kart POI table in case the combination of osm_id and osm_type is already present.

        Args:
            row_cnt (int): Number of rows to process
            category (str): Category of POIs to read
        """

        # Update the POIs if combination of OSM_ID and OSM_TYPE are already in the database and the attributes have changed
        for i in range(0, row_cnt, self.batch_size):
            sql_update_poi = f"""
                UPDATE kart_poi_goat.poi p
                SET name = s.name,
                "operator" = s."operator",
                street = s.street,
                housenumber = s.housenumber,
                zipcode = s.zipcode,
                phone = s.phone,
                email = s.email,
                website = s.website,
                capacity = s.capacity,
                opening_hours = s.opening_hours,
                wheelchair = s.wheelchair,
                tags = s.tags,
                geom = s.geom,
                category = s.category
                FROM (
                    SELECT * 
                    FROM poi_to_seed n 
                    ORDER BY osm_id 
                    LIMIT {self.batch_size} 
                    OFFSET {i}
                ) s
                WHERE p.osm_id = s.osm_id
                AND p.osm_type = s.osm_type
                AND (
                    p.name != s.name
                    OR p."operator" != s."operator"
                    OR p.street != s.street
                    OR p.housenumber != s.housenumber
                    OR p.zipcode != s.zipcode
                    OR p.phone != s.phone
                    OR p.email != s.email
                    OR p.website != s.website
                    OR p.capacity != s.capacity
                    OR p.opening_hours != s.opening_hours
                    OR p.wheelchair != s.wheelchair
                    OR p.tags != s.tags
                    OR ST_ASTEXT(p.geom) != ST_ASTEXT(s.geom)
                    OR p.category != s.category
                );
            """
            self.db.perform(sql_update_poi)

            # Print the number of rows processed
            processed = i + self.batch_size
            if processed > row_cnt:
                processed = row_cnt

            print_info(
                f"Processed UPDATE for {processed} rows out of {row_cnt} rows for category {category}"
            )

            # Commit to repository of max_commit_size rows have been reached or when all rows have been processed
            if processed % self.max_commit_size == 0 or processed == row_cnt:
                print_info(f"Commit changes for category {category} started")
                self.prepare_kart.commit(
                    f"Automatically updated category {category} with {processed} rows"
                )
                print_info(f"Commit changes for category {category} finished")

    def subscribe_osm(self):

        # Prepare a fresh kart repo
        self.prepare_kart.prepare_kart()

        # Create a new kart branch
        branch_name = uuid4().hex
        self.prepare_kart.create_new_branch(branch_name)
        self.prepare_kart.checkout_branch(branch_name)
        # Get categories to update
        sql_get_categories_to_update = """
            SELECT DISTINCT category
            FROM kart_poi_goat.data_subscription
            WHERE source = 'OSM';
        """
        categories = self.db.select(sql_get_categories_to_update)
        categories = [category[0] for category in categories]

        # Update each category and create for each a new kart commit
        categories = ["bar"]
        for category in categories:
            # Perform integration
            self.read_poi(category)
            row_cnt = self.get_row_count(category)
            self.insert_poi(row_cnt, category)
            self.update_poi(row_cnt, category)

        self.prepare_kart.push(branch_name=branch_name)
        
        # Create body message for PR 
        categories_str = "\n".join(categories)
        pr_body = f"""
            This PR was created automatically by the OSM POI subscription workflow. 
            It contains the latest POIs from OSM for the following categories:
            {categories_str}
        """
        # Create PR on main branch
        self.prepare_kart.create_pull_request(
            branch_name=branch_name,
            base_branch="main",
            title="Automated PR for OSM POIs",
            body=pr_body,
        )
        self.db.conn.close()

def main():
    db = Database(settings.LOCAL_DATABASE_URI)
    subscription = Subscription(db=db)
    subscription.subscribe_osm()


if __name__ == "__main__":
    main()
