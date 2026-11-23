CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);
CREATE TABLE public.demand_configs (
    id integer NOT NULL,
    game_id integer NOT NULL,
    kind character varying(16) NOT NULL,
    value integer,
    initial_value integer,
    step_week integer,
    step_value integer,
    slope_per_week numeric(10,4),
    start_week integer,
    cap integer,
    base integer,
    amplitude integer,
    period_weeks integer,
    phase numeric(10,4),
    distribution character varying(16),
    mean numeric(10,4),
    stdev numeric(10,4),
    min_value integer,
    max_value integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.demand_configs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.demand_configs_id_seq OWNED BY public.demand_configs.id;
CREATE TABLE public.demand_series (
    id integer NOT NULL,
    game_id integer NOT NULL,
    week integer NOT NULL,
    quantity integer NOT NULL
);
CREATE SEQUENCE public.demand_series_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.demand_series_id_seq OWNED BY public.demand_series.id;
CREATE TABLE public.game_configs (
    id integer NOT NULL,
    game_id integer NOT NULL,
    stage_count integer NOT NULL,
    pause_on_disconnect boolean NOT NULL,
    bot_fill_empty_roles boolean NOT NULL,
    currency_symbol character varying(4) NOT NULL,
    role_assignment_mode character varying(20) NOT NULL,
    preset_name character varying(32),
    show_true_customer_demand_to_all boolean NOT NULL,
    show_neighbour_inventory boolean NOT NULL,
    show_all_inventories boolean NOT NULL,
    show_supply_line_prominently boolean NOT NULL,
    show_running_cost_to_players boolean NOT NULL,
    show_leaderboard_during_game boolean NOT NULL,
    max_order_quantity integer,
    theta numeric(6,4) NOT NULL,
    alpha numeric(6,4) NOT NULL,
    beta numeric(6,4) NOT NULL,
    target_stock_multiplier numeric(6,4) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.game_configs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.game_configs_id_seq OWNED BY public.game_configs.id;
CREATE TABLE public.games (
    id integer NOT NULL,
    public_id character varying(32) NOT NULL,
    room_code character varying(8) NOT NULL,
    rng_seed bigint NOT NULL,
    duration_weeks integer NOT NULL,
    weeks_played integer NOT NULL,
    ended_early boolean NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone NOT NULL,
    chain_total_cost numeric(12,2) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    host_user_id integer
);
CREATE SEQUENCE public.games_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.games_id_seq OWNED BY public.games.id;
CREATE TABLE public.participants (
    id integer NOT NULL,
    game_id integer NOT NULL,
    alias character varying(4) NOT NULL,
    role character varying(16),
    display_name character varying(24) NOT NULL,
    participant_type character varying(12) NOT NULL,
    is_bot boolean NOT NULL,
    user_id integer,
    guest_identity character varying(64),
    bullwhip_ratio numeric(10,4),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.participants_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.participants_id_seq OWNED BY public.participants.id;
CREATE TABLE public.role_configs (
    id integer NOT NULL,
    game_id integer NOT NULL,
    role character varying(16) NOT NULL,
    initial_inventory integer NOT NULL,
    initial_backlog integer NOT NULL,
    shipping_delay_weeks integer NOT NULL,
    information_delay_weeks integer NOT NULL,
    initial_pipeline_quantity integer NOT NULL,
    initial_order_in_pipeline integer NOT NULL,
    holding_cost_per_unit_week numeric(12,2) NOT NULL,
    backlog_cost_per_unit_week numeric(12,2) NOT NULL,
    fixed_order_cost numeric(12,2) NOT NULL,
    unit_purchase_cost numeric(12,2) NOT NULL,
    starting_capital numeric(12,2) NOT NULL,
    production_delay_weeks integer,
    production_capacity_per_week integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.role_configs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.role_configs_id_seq OWNED BY public.role_configs.id;
CREATE TABLE public.user_stats (
    user_id integer NOT NULL,
    games_played integer NOT NULL,
    weeks_played integer NOT NULL,
    total_cost numeric(12,2) NOT NULL,
    avg_cost_per_week numeric(12,2) NOT NULL,
    best_game_id integer,
    bullwhip_avg numeric(10,4),
    games_as_retailer integer NOT NULL,
    games_as_wholesaler integer NOT NULL,
    games_as_distributor integer NOT NULL,
    games_as_factory integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.users (
    id integer NOT NULL,
    firebase_uid character varying(128) NOT NULL,
    display_name character varying(24),
    email character varying(200),
    photo_url character varying(500),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;
CREATE TABLE public.weeks (
    id integer NOT NULL,
    game_id integer NOT NULL,
    week integer NOT NULL,
    role character varying(16) NOT NULL,
    opening_inventory integer NOT NULL,
    opening_backlog integer NOT NULL,
    arrived integer NOT NULL,
    incoming_order integer NOT NULL,
    obligation integer NOT NULL,
    shipped integer NOT NULL,
    unfulfilled integer NOT NULL,
    closing_inventory integer NOT NULL,
    closing_backlog integer NOT NULL,
    supply_line_after integer NOT NULL,
    orders_in_flight_after integer NOT NULL,
    order_qty integer NOT NULL,
    was_bot boolean NOT NULL,
    was_forced boolean NOT NULL,
    holding_cost numeric(12,2) NOT NULL,
    backlog_cost numeric(12,2) NOT NULL,
    fixed_order_cost numeric(12,2) NOT NULL,
    purchase_cost numeric(12,2) NOT NULL,
    total_week_cost numeric(12,2) NOT NULL,
    cumulative_cost numeric(12,2) NOT NULL,
    production_started integer,
    production_queued integer
);
CREATE SEQUENCE public.weeks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.weeks_id_seq OWNED BY public.weeks.id;
ALTER TABLE ONLY public.demand_configs ALTER COLUMN id SET DEFAULT nextval('public.demand_configs_id_seq'::regclass);
ALTER TABLE ONLY public.demand_series ALTER COLUMN id SET DEFAULT nextval('public.demand_series_id_seq'::regclass);
ALTER TABLE ONLY public.game_configs ALTER COLUMN id SET DEFAULT nextval('public.game_configs_id_seq'::regclass);
ALTER TABLE ONLY public.games ALTER COLUMN id SET DEFAULT nextval('public.games_id_seq'::regclass);
ALTER TABLE ONLY public.participants ALTER COLUMN id SET DEFAULT nextval('public.participants_id_seq'::regclass);
ALTER TABLE ONLY public.role_configs ALTER COLUMN id SET DEFAULT nextval('public.role_configs_id_seq'::regclass);
ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);
ALTER TABLE ONLY public.weeks ALTER COLUMN id SET DEFAULT nextval('public.weeks_id_seq'::regclass);
ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);
ALTER TABLE ONLY public.demand_configs
    ADD CONSTRAINT pk_demand_configs PRIMARY KEY (id);
ALTER TABLE ONLY public.demand_series
    ADD CONSTRAINT pk_demand_series PRIMARY KEY (id);
ALTER TABLE ONLY public.game_configs
    ADD CONSTRAINT pk_game_configs PRIMARY KEY (id);
ALTER TABLE ONLY public.games
    ADD CONSTRAINT pk_games PRIMARY KEY (id);
ALTER TABLE ONLY public.participants
    ADD CONSTRAINT pk_participants PRIMARY KEY (id);
ALTER TABLE ONLY public.role_configs
    ADD CONSTRAINT pk_role_configs PRIMARY KEY (id);
ALTER TABLE ONLY public.user_stats
    ADD CONSTRAINT pk_user_stats PRIMARY KEY (user_id);
ALTER TABLE ONLY public.users
    ADD CONSTRAINT pk_users PRIMARY KEY (id);
ALTER TABLE ONLY public.weeks
    ADD CONSTRAINT pk_weeks PRIMARY KEY (id);
ALTER TABLE ONLY public.demand_configs
    ADD CONSTRAINT uq_demand_configs_game_id UNIQUE (game_id);
ALTER TABLE ONLY public.demand_series
    ADD CONSTRAINT uq_demand_series_game_id_week UNIQUE (game_id, week);
ALTER TABLE ONLY public.game_configs
    ADD CONSTRAINT uq_game_configs_game_id UNIQUE (game_id);
ALTER TABLE ONLY public.games
    ADD CONSTRAINT uq_games_public_id UNIQUE (public_id);
ALTER TABLE ONLY public.games
    ADD CONSTRAINT uq_games_room_code_started_at UNIQUE (room_code, started_at);
ALTER TABLE ONLY public.role_configs
    ADD CONSTRAINT uq_role_configs_game_id_role UNIQUE (game_id, role);
ALTER TABLE ONLY public.weeks
    ADD CONSTRAINT uq_weeks_game_id_week_role UNIQUE (game_id, week, role);
CREATE INDEX ix_games_host_user_id ON public.games USING btree (host_user_id);
CREATE INDEX ix_games_room_code ON public.games USING btree (room_code);
CREATE INDEX ix_participants_game_id_role ON public.participants USING btree (game_id, role);
CREATE INDEX ix_participants_user_id ON public.participants USING btree (user_id);
CREATE INDEX ix_user_stats_best_game_id ON public.user_stats USING btree (best_game_id);
CREATE UNIQUE INDEX ix_users_firebase_uid ON public.users USING btree (firebase_uid);
CREATE INDEX ix_weeks_game_id_role ON public.weeks USING btree (game_id, role);
ALTER TABLE ONLY public.demand_configs
    ADD CONSTRAINT fk_demand_configs_game_id FOREIGN KEY (game_id) REFERENCES public.games(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.demand_series
    ADD CONSTRAINT fk_demand_series_game_id FOREIGN KEY (game_id) REFERENCES public.games(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.game_configs
    ADD CONSTRAINT fk_game_configs_game_id FOREIGN KEY (game_id) REFERENCES public.games(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.games
    ADD CONSTRAINT fk_games_host_user_id FOREIGN KEY (host_user_id) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.participants
    ADD CONSTRAINT fk_participants_game_id FOREIGN KEY (game_id) REFERENCES public.games(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.participants
    ADD CONSTRAINT fk_participants_user_id FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.role_configs
    ADD CONSTRAINT fk_role_configs_game_id FOREIGN KEY (game_id) REFERENCES public.games(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.user_stats
    ADD CONSTRAINT fk_user_stats_best_game_id FOREIGN KEY (best_game_id) REFERENCES public.games(id) ON DELETE SET NULL;
ALTER TABLE ONLY public.user_stats
    ADD CONSTRAINT fk_user_stats_user_id FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.weeks
    ADD CONSTRAINT fk_weeks_game_id FOREIGN KEY (game_id) REFERENCES public.games(id) ON DELETE CASCADE;
