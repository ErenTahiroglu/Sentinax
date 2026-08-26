import os
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

def get_supabase_client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") # Use service role for backend worker
    if not url or not key:
        raise ValueError("Supabase URL or Key missing in environment")
    return create_client(url, key)

def get_allowed_assets():
    """
    Fetches the whitelist of active assets from Supabase.
    """
    supabase = get_supabase_client()
    response = supabase.table("allowed_assets").select("symbol, asset_type").eq("is_active", True).execute()
    return response.data
