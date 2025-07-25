import requests as r
import json
from datetime import datetime, timedelta, timezone, date
import pandas as pd
import numpy as np
import psycopg2
import time
import os
import threading
import subprocess
import logging
import traceback
import smtplib
import gspread
from concurrent import futures
from sqlalchemy import create_engine
from google.oauth2 import service_account
from openpyxl import load_workbook
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from adsim_config import adsim_token, host, port, dbname, user, password, engine
from adsim_dicts import expected_columns, needed_columns
import math

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename="script.log",
    filemode="a",
)

# Initialize report
report = {
    "status": "success",
    "operations": [],
    "errors": [],
    "warning": []
}

def log_operation(operation, status, details=None):
    report["operations"].append({
        "operation": operation,
        "status": status,
        "details": details,
    })

def log_error_report(error):
    report["errors"].append({
        "error_type": type(error).__name__,
        "error_message": str(error),
        "traceback": traceback.format_exc(),
    })
    report["status"] = "failed"

def log_warning_report(warning_message, details=None):
    report["warnings"].append({
        "warning_message": warning_message,
        "details": details,
    })

def save_report(report):
    """
    Saves the report to a JSON file in a folder named 'reports' only if status is "failed".
    If the folder doesn't exist, it creates it.
    """
    # Only proceed if status is "failed"
    if report.get("status") != "failed":
        print("Report not saved as status is not 'failed'")
        return
    
    # Define the folder name
    reports_folder = Path("reports")
    
    # Create the folder if it doesn't exist
    reports_folder.mkdir(exist_ok=True)
    
    # Generate a timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Define the file path
    file_path = reports_folder / f"script_report_{timestamp}.json"
    
    # Save the report to the file
    with open(file_path, "w") as f:
        json.dump(report, f, indent=4)
    
    logging.info(f"Report saved to {file_path}")

end = datetime.today()
end_date = end.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

logs_end = date.today()
logs_end_str = logs_end.strftime("%Y-%m-%d")

start = end - timedelta(minutes=45)
start_date = start.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
logs_start_str = start.strftime("%Y-%m-%d")

deals_url = f"https://api.adsim.co/crm-r/api/v2/deals?start={start_date}&end={end_date}"
logs_url = f'https://api.adsim.co/crm-r/api/v2/deals/steps/logs?enterDateStart={logs_end_str}'
proposals_url = f'https://api.adsim.co/crm-r/api/v2/deals/proposals?start={start_date}&end={end_date}'
organization_url = f"https://api.adsim.co/crm-r/api/v2/entities?start={start_date}&end={end_date}"
activities_url = f"https://api.adsim.co/crm-r/api/v2/activity?start={start_date}"

headers = {
    "authorization" : f"Bearer {adsim_token}",
}

scopes = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]
json_file = r"./json_files/credentials.json"

def login():
    credentials = service_account.Credentials.from_service_account_file(json_file)
    scoped_credentials = credentials.with_scopes(scopes)
    gc = gspread.authorize(scoped_credentials)
    return gc

def replace_nat_with_none(df):
    """
    Replaces NaT/NaN values in datetime and date columns of a DataFrame with None.
    Replaces empty strings "" with None in object columns.
    Replaces NaN numeric objects with None.

    Args:
        df (pd.DataFrame): The DataFrame to process.

    Returns:
        pd.DataFrame: The DataFrame with NaT/NaN values replaced by None.
    """
    # Handle datetime64 and numeric columns
    for col in df.select_dtypes(include=['datetime64', 'datetime64[ns]', 'number']).columns:
        df[col] = df[col].apply(lambda x: None if pd.isna(x) else x)

    # Handle object columns (strings)
    for col in df.select_dtypes(include=['object']).columns:
      df[col] = df[col].apply(lambda x: None if pd.isna(x) or x == "" else x)
    
    # Handle date columns separately
    for col in df.columns:
        if all(isinstance(x, date) for x in df[col].dropna()):
            df[col] = df[col].apply(lambda x: None if pd.isna(x) else x)

    return df

def find_differences(df1, df2, id_column, columns_to_check):
    """
    Find rows to update and insert by comparing two dataframes.
    Modified to handle CSV data as input and partial updates.
    Now treats NaT the same as NaN.

    Parameters:
        df1 (pd.DataFrame): The first dataframe (e.g., current data - CSV).
        df2 (pd.DataFrame): The second dataframe (e.g., new data - from API or CSV).
        id_column (str): The name of the ID column to match on.
        columns_to_check (list): List of column names to compare.

    Returns:
        dict: A dictionary with two DataFrames:
            - "rows_to_update": A DataFrame that contains:
                - id_column: The id
                - List of columns with changes, and its new values.
            - "rows_to_insert": Rows where IDs exist only in df2.
    """

    if isinstance(df2, pd.Index):
        df2 = df2.to_frame()

    # Log columns before the merge
    print(f"Columns in df1: {df1.columns.tolist()}")
    print(f"Columns in df2: {df2.columns.tolist()}")

    # Ensure columns_to_check exist in both DataFrames
    common_columns = set(columns_to_check).intersection(set(df1.columns)).intersection(set(df2.columns))
    if len(common_columns) != len(columns_to_check):
        missing_columns = set(columns_to_check) - common_columns
        print(f"Some columns to check are missing: {missing_columns}")

    columns_to_check = list(common_columns)

    # Proceed with the merge
    merged = pd.merge(df1, df2, on=id_column, how='outer', suffixes=('_old', '_new'))

    # Log columns after the merge
    print(f"Columns in merged DataFrame: {merged.columns.tolist()}")

    # Identify rows with changes
    rows_with_changes_mask = merged[id_column].notna()

    # Create a list to store information about rows to update
    rows_to_update = []

    for index, row in merged[rows_with_changes_mask].iterrows():
        changed_columns = {}
        for col in columns_to_check:
            old_val = row[f"{col}_old"]
            new_val = row[f"{col}_new"]

            # Explicitly handle NaT (convert to None)
            if pd.isna(old_val):
                old_val = None
            if pd.isna(new_val):
                new_val = None

            if new_val != old_val :
                # If one of them is None and the other is an empty string, consider them equal
                if not (old_val is None and new_val == "" or old_val == "" and new_val is None):
                    changed_columns[col] = new_val

        if changed_columns:  # If any columns changed
            changed_columns[id_column] = row[id_column]
            rows_to_update.append(changed_columns)

    # Convert the list of dictionaries to a DataFrame
    if rows_to_update:
        rows_to_update = pd.DataFrame(rows_to_update)
    else:
        rows_to_update = pd.DataFrame(columns=[id_column] + columns_to_check)  # empty dataframe

    # Rows where the ID exists only in df2 (new rows to insert)
    rows_to_insert = merged[
        merged[[f"{col}_old" for col in columns_to_check]].isna().all(axis=1) &  # "_old" columns are NaN or NaT
        merged[[f"{col}_new" for col in columns_to_check]].notna().any(axis=1)  # "_new" columns has at least one value
    ]

    # Remove suffixes from columns in rows_to_insert
    rows_to_insert = rows_to_insert[[id_column] + [f"{col}_new" for col in columns_to_check]]
    rows_to_insert.columns = [id_column] + columns_to_check  # Rename columns to remove suffixes

    return {
        "rows_to_update": rows_to_update,
        "rows_to_insert": rows_to_insert
    }

def update_or_insert_rows(conn, cursor, table_name, id_column, columns_to_check, rows_to_update, rows_to_insert):
    """
    Updates or inserts rows in a database table, with enhanced error tracking.
    """
    if not isinstance(rows_to_update, pd.DataFrame):
        log_operation(f"rows_to_update is not a DataFrame. It is a {type(rows_to_update)}", "warning")
        return

    if not isinstance(rows_to_insert, pd.DataFrame):
        log_operation(f"rows_to_insert is not a DataFrame. It is a {type(rows_to_insert)}", "warning")
        return
    
    rows_to_update = replace_nat_with_none(rows_to_update)
    rows_to_insert = replace_nat_with_none(rows_to_insert)

    if rows_to_update.empty and rows_to_insert.empty:
        log_operation(f"No updates or inserts needed for table {table_name}.", "warning")
        return

    start_time = time.time()

    # Update Logic
    if table_name != "historico":
        if not rows_to_update.empty:
            if id_column not in rows_to_update.columns:
                log_operation(f"id_column {id_column} not found in rows_to_update", "warning")
                return

            log_operation(f"Attempting to update rows in {table_name}.", "info")

            try:
                update_count = 0
                for index, row in rows_to_update.iterrows():
                    set_clauses = []
                    values = []
                    problematic_columns = []
                    skipped_count = 0

                    for col in columns_to_check:
                        if col in row:
                            if row[col] is None or row[col] == "" or (isinstance(row[col], (int, float)) and pd.isna(row[col])) or pd.isna(row[col]):
                                skipped_count +=1
                                continue
                            
                            if isinstance(row[col], (int, float)) and not pd.isna(row[col]):
                                if not (-9223372036854775808 <= row[col] <= 9223372036854775807):
                                    problematic_columns.append((col, row[col]))
                                    log_operation(
                                        f"Value out of range for bigint in column '{col}' during update in table '{table_name}' (ID: {row[id_column]}), Value: {row[col]}",
                                        "warning"
                                    )
                                    continue

                            # Handle date/datetime columns
                            if isinstance(row[col], pd.Timestamp):
                                values.append(row[col].to_pydatetime())
                            elif isinstance(row[col], date):
                                values.append(row[col])
                            else:
                                values.append(row[col])

                            set_clauses.append(f"{col} = %s")

                    if not set_clauses:
                        log_operation(f"No columns to update for row {row[id_column]} in {table_name} after skipping empty values.", "warning")
                        continue
                    
                    if problematic_columns:
                        log_operation(f"Skipping row update (ID: {row[id_column]}) in table '{table_name}' due to values out of range in columns: {problematic_columns}", "warning")
                        continue

                    set_clause = ", ".join(set_clauses)
                    values.append(row[id_column])
                    sql_update = f"UPDATE {table_name} SET {set_clause} WHERE {id_column} = %s"
                    
                    try:
                        cursor.execute(sql_update, values)
                        update_count += 1
                    except Exception as e:
                        conn.rollback()
                        log_error_report(e)
                        log_operation(f"failed to update data into {table_name} for row: {row[id_column]}", "failed", str(e))

                conn.commit()
                log_operation(f"Successfully updated {update_count} rows in {table_name}.", "success")

            except Exception as e:
                conn.rollback()
                log_error_report(e)
                log_operation(f"failed to update data into {table_name}", "failed", str(e))

            log_operation(f"Update operations for table {table_name} completed.", "success")
    
    else: 
        log_operation(f"Skipping update operations for table {table_name} as requested.", "warning")

    # Insert Logic (improved NaN/NaT handling)
    if not rows_to_insert.empty:           
        if id_column not in rows_to_insert.columns:
            log_operation(f"id_column {id_column} not found in rows_to_insert", "warning")
            return

        missing_columns = set(columns_to_check) - set(rows_to_insert.columns)
        if missing_columns:
            log_operation(f"Missing columns in rows_to_insert: {missing_columns}", "warning")
            return

        log_operation(f"Attempting to insert {len(rows_to_insert)} rows into {table_name}.", "info")

        try:
            insert_data = []
            problematic_rows = []
            for index, row in rows_to_insert.iterrows():
                columns = [id_column] + columns_to_check
                values = []
                row_problematic_columns = []

                for col in columns:
                    # Handle None, empty strings, and NaN explicitly.
                    if row[col] is None or row[col] == "" or pd.isna(row[col]):
                        values.append(None) # Treat as null
                        continue

                    if isinstance(row[col], (int, float)):
                        if not (-9223372036854775808 <= row[col] <= 9223372036854775807):
                            row_problematic_columns.append((col, row[col]))
                            log_operation(
                                f"Value out of range for bigint in column '{col}' during insert in table '{table_name}' (ID: {row[id_column]}), Value: {row[col]}",
                                "warning"
                            )
                            continue
                        values.append(row[col])

                    elif isinstance(row[col], pd.Timestamp):
                        values.append(row[col].to_pydatetime())
                    elif isinstance(row[col], date):
                        values.append(row[col])
                    else:
                        values.append(row[col])

                if row_problematic_columns:
                    problematic_rows.append((row[id_column], row_problematic_columns))
                    continue
                
                insert_data.append(tuple(values))

            if problematic_rows:
                for id, cols in problematic_rows:
                    log_operation(f"Skipping row insertion (ID: {id}) in table '{table_name}' due to values out of range or invalid type in columns: {cols}", "warning")

            placeholders = ", ".join(["%s"] * len(columns))
            sql_insert = f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({placeholders})"

            if insert_data:
                psycopg2.extras.execute_batch(cursor, sql_insert, insert_data)
                conn.commit()
                log_operation(f"Successfully inserted {len(insert_data)} rows into {table_name}.", "success")
            else:
                log_operation(f"No valid rows to insert in {table_name}.", "warning")

        except Exception as e:
            conn.rollback()
            log_error_report(e)
            log_operation(f"failed to insert data to {table_name}", "failed", str(e))

        log_operation(f"Insert operations for table {table_name} completed.", "success")



    end_time = time.time()
    elapsed_time = end_time - start_time
    log_operation(f"update_or_insert_rows for table {table_name} took {elapsed_time:.2f} seconds.", "info")


def compare_and_update_table(cursor, conn, table_name, id_column, columns_to_check, df1, df2):
    result = find_differences(df1, df2, id_column, columns_to_check)
    time.sleep(1)
    update_or_insert_rows(conn,cursor, table_name, id_column, columns_to_check, result["rows_to_update"], result["rows_to_insert"])

def extract_adsim_data(url, max_retries=3, timeout_seconds=30, retry_delay_seconds=5):
    """
    Extracts data from AdSim API with timeout and retry logic.

    Args:
        url (str): The API endpoint URL.
        max_retries (int): Maximum number of times to retry the request.
        timeout_seconds (int): Timeout for the API request in seconds.
        retry_delay_seconds (int): Delay between retries in seconds.

    Returns:
        pd.DataFrame: DataFrame containing the API response data, or an empty DataFrame on failure.
    """
    for attempt in range(max_retries):
        try:
            # Make the API request with a timeout
            response = r.get(url, headers=headers, timeout=timeout_seconds)
            response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)

            # Print the response text and content type for debugging
            # print(response.text)
            print(response.headers.get('Content-Type'))

            # Read the response text
            ndjson_text = response.text

            # Split the text into individual lines
            ndjson_lines = ndjson_text.strip().split('\n')

            # Parse each line as a JSON object
            data_list = []
            for line in ndjson_lines:
                if line.strip():  # Skip empty lines
                    try:
                        data_list.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        print(f"JSONDecodeError on line: {line}")
                        print(e)
                        log_warning_report(f"JSONDecodeError while parsing API response from {url}", f"Line: {line}, Error: {e}")


            # Convert the list of JSON objects into a DataFrame
            df = pd.DataFrame(data_list)
            log_operation(f"Successfully fetched data from {url}", "success")
            return df

        except r.exceptions.Timeout:
            print(f"Timeout occurred while fetching data from {url} (attempt {attempt + 1}/{max_retries}). Retrying in {retry_delay_seconds}s...")
            if attempt < max_retries - 1:
                time.sleep(retry_delay_seconds)
            else:
                log_error_report(TimeoutError(f"Failed to fetch data from {url} after {max_retries} attempts due to timeout."))
                return pd.DataFrame() # Return empty DataFrame after max retries

        except r.exceptions.RequestException as e:
            log_error_report(e)
            log_operation(f"Request failed for {url} (attempt {attempt + 1}/{max_retries})", "failed", str(e))
            if attempt < max_retries - 1:
                time.sleep(retry_delay_seconds)
            else:
                log_error_report(e) # Log final error
                return pd.DataFrame() # Return empty DataFrame after max retries
        
        except Exception as e: # Catch any other unexpected errors
            log_error_report(e)
            log_operation(f"An unexpected error occurred while fetching data from {url} (attempt {attempt + 1}/{max_retries})", "failed", str(e))
            if attempt < max_retries - 1:
                time.sleep(retry_delay_seconds)
            else:
                log_error_report(e) # Log final error
                return pd.DataFrame() # Return empty DataFrame after max retries
                
    return pd.DataFrame() # Should be unreachable if logic is correct, but as a fallback

def ensure_columns(df, required_columns, drop_extra_columns=True):
    """
    Ensures that the DataFrame contains all required columns.
    If any columns are missing, they are added with NaN values.
    Optionally, drops columns that are not in the required list.

    Parameters:
        df (pd.DataFrame): The DataFrame to validate.
        required_columns (list): List of required column names.
        drop_extra_columns (bool): If True, drops columns not in the required list.

    Returns:
        pd.DataFrame: The DataFrame with all required columns.
    """
    try:
        # Add missing columns with NaN values
        for column in required_columns:
            if column not in df.columns:
                df[column] = None  # Add missing column with NaN values
                log_operation(f"succesfully added missing column: {column}", "success")

        # Optionally drop extra columns
        if drop_extra_columns:
            extra_columns = [col for col in df.columns if col not in required_columns]
            if extra_columns:
                df = df.drop(columns=extra_columns)
                log_operation(f"Dropped extra columns: {extra_columns}", "success")

        return df

    except Exception as e:
        log_error_report(e)
        log_operation(f"Error in ensure_columns: {e}", "failed", str(e))
        raise  # Re-raise the exception to handle it in the calling function

def remove_decimals(value):
    """
    Removes the decimal part of a number and returns the integer part.
    If the value is NaN or None, returns None.
    """
    if pd.isna(value) or value is None:
        return None
    return math.floor(value)  # or math.trunc(value)

def convert_columns_to_int(df, columns):
    """
    Convert specified columns in a DataFrame to integer type.
    
    Parameters:
    df (pd.DataFrame): The input DataFrame.
    columns (list): List of column names to convert to int.
    
    Returns:
    pd.DataFrame: A DataFrame with updated column types.
    """
    if df.empty:
        print("Warning: DataFrame is empty. Skipping conversion.")
        return df
    
    for col in columns:
        if col not in df.columns:
            print(f"Warning: Column '{col}' does not exist in DataFrame. Skipping.")
            continue
        
        if not isinstance(df[col], pd.Series):
            print(f"Warning: Column '{col}' is not a valid Series. Skipping.")
            continue
        
        print(f"Processing column: {col}")  # Debugging
        print(f"Column type before conversion: {type(df[col])}")  # Debugging
        print(f"Column values: {df[col]}")  # Debugging

        # Check for values outside the Int64 range before conversion
        if pd.api.types.is_numeric_dtype(df[col]):
            df[col] = df[col].apply(remove_decimals)

            max_value = np.iinfo(np.int64).max
            min_value = np.iinfo(np.int64).min
            
            out_of_range_mask = (df[col].dropna() > max_value) | (df[col].dropna() < min_value)
            
            if out_of_range_mask.any():
                out_of_range_values = df.loc[out_of_range_mask, col].unique()
                print(f"Warning: Column '{col}' contains values outside the Int64 range: {out_of_range_values}")
                log_warning_report(f"Warning: Column '{col}' contains values outside the Int64 range:", f"{out_of_range_values}")
                
                # Decide on a handling strategy. Here's one example:
                df.loc[out_of_range_mask, col] = None  # Set out-of-range values to None, it will be handled as null in the DB.
                #Other options:
                #df.loc[out_of_range_mask, col] = max_value # set as max value
                #df.loc[out_of_range_mask, col] = min_value #set as min value
        
        # Convert to numeric and handle NaNs
        df[col] = pd.to_numeric(df[col], errors='coerce').astype('Int64')  # Uses nullable Int64 to handle NaNs
    return df

def remove_na(df, columns):
    if df.empty:
        print("Warning: DataFrame is empty. Skipping conversion.")
        return df
    
    for col in columns:
        if col not in df.columns:
            print(f"Warning: Column '{col}' does not exist in DataFrame. Skipping.")
            continue
        
        if not isinstance(df[col], pd.Series):
            print(f"Warning: Column '{col}' is not a valid Series. Skipping.")
            continue
        
        print(f"Processing column: {col}")  # Debugging
        print(f"Column type before conversion: {type(df[col])}")  # Debugging
        print(f"Column values: {df[col]}")  # Debugging
        
        # Convert to numeric and handle NaNs
        df[col] = pd.to_numeric(df[col], errors='coerce').astype('Int64')  # Uses nullable Int64 to handle NaNs

# Function to safely drop columns
def drop_columns(df, columns_to_drop):
    """
    Safely drops specified columns from a DataFrame if they exist.
    
    Parameters:
        df (pd.DataFrame): The DataFrame from which to drop columns.
        columns_to_drop (list): List of column names to drop.
        
    Returns:
        pd.DataFrame: The DataFrame with the specified columns dropped.
    """
    # Check which columns exist in the DataFrame
    existing_columns = [col for col in columns_to_drop if col in df.columns]
    
    # Drop the existing columns
    if existing_columns:
        df = df.drop(columns=existing_columns)
        log_operation(f"Successfully dropped columns: {existing_columns}", "success")
    else:
        log_operation(f"No columns to drop. Columns {columns_to_drop} not found in DataFrame.", "warning")
    
    return df

# Function to classify organizations
def classify_organization(conn, org_id):
    """
    Classify an Organization from the Database.

    Parameters:
        conn: Database connection object.
        org_id (int): The Organization ID.

    Returns:
        str: The organization classification ("Cliente Ausente", "Prospect", "Cliente Ativo")
    """
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT iswon FROM deals WHERE organization_id = %s", (org_id,)
        )
        org_deals = cursor.fetchall()

        if not org_deals:
            return "Cliente Ausente"
        elif all(deal[0] is False for deal in org_deals):
            return "Prospect"
        else:
            return "Cliente Ativo"
    except Exception as e:
        log_error_report(e)
        log_operation("classify_organization", "failed", str(e))
        return None
    finally:
        cursor.close()

# Function to classify deals
def classify_deal(conn, row):
    """
    Classifies a deal based on its relationship with past deals in the database.

    Args:
        conn: database connection object.
        row (dict): A row (as dict) from the deals table representing a single deal.

    Returns:
        str: The deal classification ("Cliente Novo", "Negócio Repetido", "Cliente Sazonal", "Cliente Recorrente").
    """
    cursor = conn.cursor()
    try:
        current_client = row['organization_id']
        current_value = row['negotiatedvalue']
        current_date = pd.to_datetime(row['criacao_data'], errors='coerce')

        # Handle None or NaT in current_date
        if pd.isna(current_date):  # Check for NaT or None
            return None  # Skip classification for deals with missing dates

        # Define time windows using Timestamp
        start_date_18 = pd.Timestamp(current_date - pd.DateOffset(months=18))
        start_date_2 = pd.Timestamp(current_date - pd.DateOffset(months=2))
        start_date_14 = pd.Timestamp(current_date - pd.DateOffset(months=14))
        start_date_10 = pd.Timestamp(current_date - pd.DateOffset(months=10))

        # Convert to date format for SQL comparison
        start_date_18_str = start_date_18.strftime('%Y-%m-%d')
        start_date_2_str = start_date_2.strftime('%Y-%m-%d')
        start_date_14_str = start_date_14.strftime('%Y-%m-%d')
        start_date_10_str = start_date_10.strftime('%Y-%m-%d')
        current_date_str = current_date.strftime('%Y-%m-%d')

        # Query past deals in the last 18 months
        cursor.execute(
            "SELECT COUNT(*) FROM deals WHERE organization_id = %s AND criacao_data < %s AND criacao_data >= %s",
            (current_client, current_date_str, start_date_18_str)
        )
        any_past_client_18 = cursor.fetchone()[0]

        # Query past deals with the same value in the last 2 months
        cursor.execute(
            "SELECT COUNT(*) FROM deals WHERE organization_id = %s AND negotiatedvalue = %s AND criacao_data >= %s",
            (current_client, current_value, start_date_2_str)
        )
        any_past_same_val_2 = cursor.fetchone()[0]

        # Query past deals in the last 10-14 months
        cursor.execute(
            "SELECT COUNT(*) FROM deals WHERE organization_id = %s AND criacao_data >= %s AND criacao_data < %s",
            (current_client, start_date_14_str, start_date_10_str)
        )
        any_past_sazonal = cursor.fetchone()[0]

        if any_past_client_18 == 0:
            return "Cliente Novo"
        elif any_past_same_val_2 > 0:
            return "Negócio Repetido"
        elif any_past_sazonal == 1:
            return "Cliente Sazonal"
        else:
            return "Cliente Recorrente"
    except Exception as e:
        log_error_report(e)
        log_operation("classify_deal", "failed", str(e))
        return None
    finally:
        cursor.close()

def update_deal_and_organization_status(conn):
    """
    Updates the deal_status in the 'deals' table and organization_status in the 'organization' table,
    retrieving the data from the database.

    Args:
        conn: Database connection object.
    """
    cursor = conn.cursor()
    try:
        log_operation("Starting updating deal status.", "success")

        # Fetch all deals
        cursor.execute("SELECT main_id, organization_id, negotiatedvalue, criacao_data FROM deals")
        all_deals = cursor.fetchall()
        
        deal_data = [
            {
                "main_id" : row[0],
                "organization_id": row[1],
                "negotiatedvalue" : row[2],
                "criacao_data" : row[3]
            } for row in all_deals
        ]

        # Update deal_status
        for row in deal_data:
            deal_status = classify_deal(conn, row)
            if deal_status:
                cursor.execute("UPDATE deals SET deal_status = %s WHERE main_id = %s", (deal_status, row['main_id']))
        
        conn.commit()
        log_operation("deals status update finished succesfully", "success")

        log_operation("Starting updating organization status", "success")

        # Get all organization IDs
        cursor.execute("SELECT organization_id FROM organization")
        organization_ids = [row[0] for row in cursor.fetchall()]
        
        # Update organization_status
        for org_id in organization_ids:
            org_status = classify_organization(conn, org_id)
            if org_status:
                cursor.execute("UPDATE organization SET organization_status = %s WHERE organization_id = %s", (org_status, org_id))

        conn.commit()
        log_operation("organization status update finished succesfully", "success")

    except Exception as e:
        conn.rollback()
        log_error_report(e)
        log_operation("update_deal_and_organization_status", "failed", str(e))
    finally:
        cursor.close()

def safe_merge(df1, df2, id_column, columns_to_merge, merge_type='inner'):
    """
    Safely merges two DataFrames on a specified column, ensuring the merge key is unique in df2.

    Parameters:
        df1 (pd.DataFrame): The left DataFrame.
        df2 (pd.DataFrame): The right DataFrame.
        id_column (str): The column to merge on.
        columns_to_merge (str or list): The column(s) to merge from df2.
        merge_type (str): Type of merge to perform (e.g., 'inner', 'left', 'right', 'outer').

    Returns:
        pd.DataFrame: The merged DataFrame.
    """
    # Check if id_column exists in both DataFrames
    if id_column not in df1.columns or id_column not in df2.columns:
        log_operation(f"Column '{id_column}' not found in one or both DataFrames.", "failed")
        return df1  # Return the original DataFrame if the merge key is missing

    # Check if id_column is unique in df2
    if not df2[id_column].is_unique:
        log_operation(f"Column '{id_column}' is not unique in df2. Merge aborted.", "warning")
        return df1  # Return the original DataFrame if the merge key is not unique

    # Ensure columns_to_merge is a list
    if isinstance(columns_to_merge, str):
        columns_to_merge = [columns_to_merge]

    # Check if columns_to_merge exist in df2
    missing_columns = [col for col in columns_to_merge if col not in df2.columns]
    if missing_columns:
        log_operation(f"Columns {missing_columns} not found in df2. Merge aborted.", "failed")
        return df1  # Return the original DataFrame if columns_to_merge are missing

    # Perform the merge
    try:
        merged_df = df1.merge(df2[[id_column] + columns_to_merge], on=id_column, how=merge_type)
        log_operation(f"Successfully merged DataFrames on column '{id_column}'.", "success")
        return merged_df
    except Exception as e:
        log_operation(f"Merge failed: {str(e)}", "failed")
        return df1  # Return the original DataFrame if an error occurs

def main():
    #deals table block
    try:
        df = extract_adsim_data(deals_url)
        df = ensure_columns(df, needed_columns['deals'],drop_extra_columns=False)
        df = df.rename(columns={'id': 'main_id'})

        df['registerDate'] = pd.to_datetime(df['registerDate'], errors='coerce')
        df['lastUpdateDate'] = pd.to_datetime(df['lastUpdateDate'], errors='coerce')

        df['criacao_data'] = df['registerDate'].dt.date
        df['atualizacao_data'] = df['lastUpdateDate'].dt.date
        log_operation("Fetch data from API", "success")
    except Exception as e:
        log_error_report(e)
        log_operation("Fetch data from API", "failed", str(e))        

    #pipeline script block
    try:
        pipeline = pd.json_normalize(df['pipeline'], sep='_')
        pipeline = ensure_columns(pipeline, needed_columns['pipeline'], drop_extra_columns=False)

        df['pipeline_id'] = pipeline['id']
        pipeline = pipeline.rename(columns={'id': 'pipeline_id'})
        df = drop_columns(df, columns_to_drop=['pipeline'])

        pipeline = drop_columns(pipeline, columns_to_drop=['registerDate','lastUpdateDate','startDate','endDate','notes', 'goaldeal'])

        pipeline = pipeline.drop_duplicates(subset=['pipeline_id'])

        log_operation("pipeline dataframe, succesfully created!", "success")
    except Exception as e:
        log_error_report(e)
        log_operation("error encountered while transforming pipeline dataframe", "failed", str(e))

    #users script block
    try:
        creatorUser = pd.json_normalize(df['registeredByUser'],sep='_')
        creatorUser = ensure_columns(creatorUser,needed_columns['users'], drop_extra_columns=False)

        responsibleUser = pd.json_normalize(df['responsibleUser'],sep='_')
        responsibleUser = ensure_columns(responsibleUser,needed_columns['users'], drop_extra_columns=False)

        df['creatorUser_id'] = creatorUser['id']
        df['responsible_id'] = responsibleUser['id']

        df = drop_columns(df, columns_to_drop=['registeredByUser','responsibleUser'])

        users = pd.concat([creatorUser,responsibleUser])

        users = users.drop_duplicates(subset=['id'])
        users = drop_columns(users, columns_to_drop=['users'])
        users = users.rename(columns={'id': 'user_id'})

        del creatorUser, responsibleUser

        log_operation("users dataframe created successfully!", "success")
    except Exception as e:
        log_error_report(e)
        log_operation("users dataframe creation failed!", "failed", str(e))
    
    #pipelineStep script block
    try:
        pipelineStep = pd.json_normalize(df['pipelineStep'],sep='_')
        pipelineStep = ensure_columns(pipelineStep, needed_columns['pipelineStep'], drop_extra_columns=False)

        df['pipelineStep_id'] = pipelineStep['id']
        pipelineStep = pipelineStep.rename(columns={'id': 'pipelineStep_id'})

        df = drop_columns(df, columns_to_drop=['pipelineStep'])

        pipelineStep = pipelineStep.drop_duplicates(subset=['pipelineStep_id'])

        pipelineStep = drop_columns(pipelineStep, columns_to_drop=['lastUpdateDate', 'registerDate'])

        pipelineStep.head()
        log_operation("pipelineStep dataframe created successfully!", "success")
    except Exception as e:
        log_error_report(e)
        log_operation("pipelineStep dataframe creation failed!", "failed", str(e))
    
    #company script block
    try:
        company = pd.json_normalize(df['company'], sep='_')
        company = ensure_columns(company, needed_columns['company'], drop_extra_columns=False)
        df['company_id'] = company['id']

        df = drop_columns(df, columns_to_drop=['company', 'logoUrl'])

        company = company.rename(columns={'id': 'company_id'})
        company = company.drop_duplicates(subset=['company_id'])

        company.head()
        log_operation("pipelineStep dataframe created successfully!", "success")
    except Exception as e:
        log_error_report(e)
        log_operation("company dataframe creation failed!", "failed", str(e))

    #organization script block
    try:
        organization2 = pd.json_normalize(df['organization'],sep='_')
        organization2 = ensure_columns(organization2, needed_columns['organization'], drop_extra_columns=False)

        df['organization_id'] = organization2['id']

        organization = extract_adsim_data(organization_url)
        organization = ensure_columns(organization, needed_columns['organization'], drop_extra_columns=False)

        organization = organization.rename(columns={'id' : 'organization_id'})
        organization2 = organization2.rename(columns={'id' : 'organization_id'})
        df = drop_columns(df, columns_to_drop=['organization'])

        organization_phoneNumbers = organization.explode('phoneNumbers')[['organization_id', 'phoneNumbers']]
        organization_phoneNumbers = ensure_columns(organization_phoneNumbers, needed_columns['organization_phonenumbers'], drop_extra_columns=False)
        organization_phoneNumbers = organization_phoneNumbers.dropna(subset=['phoneNumbers'])
        organization_phoneNumbers = organization_phoneNumbers.drop_duplicates(subset=['phoneNumbers'])

        organization_emails = organization.explode('emails')[['organization_id','emails']]
        organization_emails = ensure_columns(organization_emails, needed_columns['organization_emails'], drop_extra_columns=False)
        organization_emails = organization_emails.dropna(subset=['emails'])
        organization_emails = organization_emails.drop_duplicates(subset=['emails'])

        organization_company = pd.json_normalize(organization['company'], sep='_')
        organization_company = ensure_columns(organization_company, needed_columns['gf_deals'], drop_extra_columns=False)
        organization['company_id'] = organization_company['id']

        segments = organization.explode('segments')[['segments']]
        segments = pd.json_normalize(segments['segments'], sep='_')
        segments = ensure_columns(segments, needed_columns['segments'], drop_extra_columns=False)
        organization['segment_id'] = segments['id']
        segments = segments.rename(columns={'id': 'segment_id'})
        segments = segments.dropna(subset=['segment_id'])
        segments = segments.drop_duplicates(subset=['segment_id'])

        segments2 = organization2.explode('segments')[['segments']]
        segments2 = pd.json_normalize(segments2['segments'], sep='_')
        segments2 = ensure_columns(segments2, needed_columns['segments'], drop_extra_columns=False)
        organization2['segment_id'] = segments2['id']

        portfolios2 = organization2.explode('customerPortfolios')[['customerPortfolios']]
        portfolios2 = pd.json_normalize(portfolios2['customerPortfolios'])
        portfolios2 = ensure_columns(portfolios2, needed_columns['portfolios'], drop_extra_columns=False)
        organization['portfolio_id'] = portfolios2['id']

        portfolios = organization.explode('customerPortfolios')[['customerPortfolios']]
        portfolios = pd.json_normalize(portfolios['customerPortfolios'])
        portfolios = ensure_columns(portfolios, needed_columns['portfolios'], drop_extra_columns=False)
        organization['portfolio_id'] = portfolios['id']
        portfolios = portfolios.rename(columns={'id': 'portfolio_id', 'userEmail' : 'login'})
        portfolios = portfolios.dropna(subset=['portfolio_id'])
        portfolios = portfolios.drop_duplicates(subset='portfolio_id')
        portfolios.loc[portfolios['companyId'] == 12.0, 'companyId'] = 782.0

        portfolios = safe_merge(portfolios, users, id_column='user_id', columns_to_merge=['login'], merge_type='left')
        portfolios = drop_columns(portfolios, columns_to_drop=['login', 'userFullName'])

        portfolios['registerDate'] = pd.to_datetime(portfolios['registerDate'], errors='coerce')
        portfolios['lastUpdateDate'] = pd.to_datetime(portfolios['lastUpdateDate'], errors='coerce')
        portfolios['criacao_data'] = portfolios['registerDate'].dt.date
        portfolios['atualizacao_data'] = portfolios['lastUpdateDate'].dt.date

        organization = drop_columns(organization, columns_to_drop=['emails','phoneNumbers','company', 'notes', 'specialFields', 'links', 'segments', 'customerPortfolios'])
        organization2 = drop_columns(organization2, columns_to_drop=['emails','phoneNumbers','company_name','company_cnpjCpf', 'company_logoUrl', 'notes', 'specialFields', 'links', 'segments', 'customerPortfolios'])
        organization = pd.concat([organization2,organization], axis=0, ignore_index=True)

        organization['registerDate'] = pd.to_datetime(organization['registerDate'], errors='coerce')
        organization['criacao_data'] = organization['registerDate'].dt.date

        organization = organization.drop_duplicates(subset=['organization_id'])
        organization = organization.dropna(subset=['organization_id'])

        del portfolios2, organization2, segments2

        log_operation("organization, segments, emails, phone dataframes created succesfully!", "success")
    except Exception as e:
        log_error_report(e)
        log_operation("organization, segments, emails, phone dataframes dataframe creation failed!", "failed", str(e))

    #products script block
    try:
        products = df.explode('products')[['products']]
        products = pd.json_normalize(products['products'], sep='_')
        products = ensure_columns(products, needed_columns['products'], drop_extra_columns=False)

        df['products_id'] = products['id']

        products = products.rename(columns={'id' : 'product_id'})
        df = drop_columns(df, columns_to_drop=['products'])

        products = drop_columns(products, columns_to_drop=['tags', 'notes', 'value', 'endDate', 'isActive', 'companyId',
                                        'isDeleted', 'startDate', 'companyGroupId', 'lastUpdateDate',
                                        'isControlQuotas', 'isControlBalance', 'isInformativeValue',
                                        'isProposalAddItems', 'dealProductDiscount', 'dealProductQuantity',
                                        'dealProductUnitValue', 'dealProductTotalValue', 'isUnitValueOverPiTable',
                                        'isAvailableOnEmidiaPortal', 'isDigitalProposalAddItems', 'isProposalValueOnCurrentTable',
                                        'isAutomaticDistributedScheduling', 'isProposalDistributeProductsByPeriod', 'registerDate'])
        products = products.rename(columns={'name' : 'product_name'})

        products = products.dropna(subset=['product_id'])
        products = products.drop_duplicates(subset=['product_id'])
        log_operation("products dataframe created succesfully!", "success")
    except Exception as e:
        log_error_report(e)
        log_operation("products dataframe creation failed!", "failed", str(e))

    #dealtype script block
    try:
        dealType = pd.json_normalize(df['dealType'], sep='_')
        dealType = ensure_columns(dealType, needed_columns['dealType'], drop_extra_columns=False)

        df['dealType_id'] = dealType['id']
        df = drop_columns(df, columns_to_drop=['dealType'])

        dealType = dealType.rename(columns={'id' : 'dealType_id'})
        dealType = drop_columns(dealType, columns_to_drop=['company_id', 'company_name', 'company_cnpjCpf', 'company_logoUrl', 'company'])
        dealType = dealType.drop_duplicates(subset=['dealType_id'])
        dealType = dealType.dropna(subset=['dealType_id'])

        dealType.head()        
        log_operation("dealtype dataframe created succesfully!", "success")
    except Exception as e:
        log_error_report(e)
        log_operation("dealtype dataframe creation failed!", "failed", str(e))

    #dues script block
    try:    
        dues = df.explode('dues')[['main_id', 'dues']]
        dues = pd.json_normalize(dues.to_dict(orient='records'))

        # **Error Handling and Debugging:**
        if dues.empty:
          log_operation("dues DataFrame is empty after json_normalize. Skipping column renaming.", "warning")
          # Continue with the code since it is not critical to stop here
        else:
            # Check if columns are strings before applying .str accessor:
            if isinstance(dues.columns, pd.Index) and all(isinstance(col, str) for col in dues.columns):
                dues.columns = dues.columns.str.replace('dues.', '', regex=False)
                dues.columns = dues.columns.str.replace('.', '_', regex=False)
                dues.columns = dues.columns.str.replace('userId', 'user_id', regex=False)
            else:
                log_operation(f"dues.columns is not a string-only index. Found type: {type(dues.columns)}", "warning", f"Columns are {dues.columns}")
                
            #if all columns are not string, then you need to convert:
            if not all(isinstance(col, str) for col in dues.columns):
              new_columns = [str(col) for col in dues.columns]
              dues.columns = new_columns
              log_operation("dues.columns has been converted to string", "warning")

        dues = ensure_columns(dues, needed_columns['dues'], drop_extra_columns=False)

        df = drop_columns(df, columns_to_drop=['dues'])

        dues = drop_columns(dues, columns_to_drop=['dealId', 'dues', 'product_name', 'product_tags', 'product_notes', 'product_value', 'product_endDate',
                                'product_isActive', 'product_companyId', 'product_isDeleted', 'product_startDate', 'product_registerDate',
                                'product_companyGroupId', 'product_lastUpdateDate', 'product_isControlQuotas', 'product_isControlBalance',
                                'product_isInformativeValue', 'product_isProposalAddItems', 'product_dealProductDiscount', 'product_dealProductQuantity',
                                'product_dealProductUnitValue', 'product_dealProductTotalValue', 'product_isUnitValueOverPiTable', 'product_isAvailableOnEmidiaPortal',
                                'product_isDigitalProposalAddItems', 'product_isProposalValueOnCurrentTable', 'product_isAutomaticDistributedScheduling', 'product_isProposalDistributeProductsByPeriod',
                                'product', 'channel', 'displayLocation', 'displayLocation_name', 'displayLocation_initials', 'channel_name', 'channel_initials'])
        
        dues = dues.dropna(subset=['id'])

        dues.loc[dues['displayLocation_id'] == 15661, 'displayLocation_id'] = 14265
        dues.loc[dues['channel_id'] == 1154, 'channel_id'] = 941
        dues.loc[dues['channel_id'] == 944, 'channel_id'] = 934
        dues.loc[dues['channel_id'] == 955, 'channel_id'] = 934   

        dues['registerDate'] = pd.to_datetime(dues['registerDate'], errors='coerce')
        dues['lastUpdateDate'] = pd.to_datetime(dues['lastUpdateDate'], errors='coerce')
        dues['criacao_data'] = dues['registerDate'].dt.date
        dues['atualizacao_data'] = dues['lastUpdateDate'].dt.date

        dues = dues.rename(columns={'id' : 'dues_id', 'userId' : 'user_id', 'companyId' : 'company_id'})

        log_operation("dues dataframe created succesfully!", "success")
    except Exception as e:
        log_error_report(e)
        log_operation("dues dataframe creation failed!", "failed", str(e))

    #person script block
    try:
        person = pd.json_normalize(df['person'])
        person = ensure_columns(person, needed_columns['person'], drop_extra_columns=False)
        person = person.rename(columns={'id' : 'person_id'})        
        df['person_id'] = person['person_id']
        df = drop_columns(df, columns_to_drop=['person'])
        person = person.dropna(subset='person_id')
        person = person.drop_duplicates(subset=['person_id'])

        person.head()
        log_operation("person dataframe created succesfully!", "success")
    except Exception as e:
        log_error_report(e)
        log_operation("person dataframe creation failed!", "failed", str(e))

    #agencies script block
    try:
        agencies = df.explode('agencies')[['agencies']]
        agencies = pd.json_normalize(agencies['agencies'], sep='_')
        agencies = ensure_columns(agencies, needed_columns['agencies'], drop_extra_columns=False)
        agencies = agencies.rename(columns={'id': 'agencia_id'})

        df['agencies_id'] = agencies['agencia_id']
        df = drop_columns(df, columns_to_drop=['agencies'])
        agencies = agencies.dropna(subset='agencia_id')

        agencia_phoneNumbers = agencies.explode('phoneNumbers')[['agencia_id', 'phoneNumbers']]
        agencia_phoneNumbers = ensure_columns(agencia_phoneNumbers, needed_columns['agencies_phonenumbers'], drop_extra_columns=False)
        agencia_phoneNumbers = agencia_phoneNumbers.dropna(subset=['phoneNumbers'])

        agencia_emails = agencies.explode('emails')[['agencia_id','emails']]
        agencia_emails = ensure_columns(agencia_emails, needed_columns['agencies_emails'], drop_extra_columns=False)
        agencia_emails = agencia_emails.dropna(subset=['emails'])

        agencies['segments_id'] = None
        agencies['portfolio_id'] = None

        agencies['registerDate'] = pd.to_datetime(agencies['registerDate'], errors='coerce')
        agencies['criacao_data'] = agencies['registerDate'].dt.date

        agencies = drop_columns(agencies, columns_to_drop=['emails','phoneNumbers','company_name','company_cnpjCpf', 'notes', 'specialFields', 'links', 'segments', 'customerPortfolios', 'company_logoUrl'])
        agencies = agencies.drop_duplicates(subset=['agencia_id'])

        agencies.head()        
        log_operation("agencies dataframe created succesfully!", "success")
    except Exception as e:
        log_error_report(e)
        log_operation("agencies dataframe creation failed!", "failed", str(e))

    #logs script block
    try:
        pf = extract_adsim_data(logs_url)
        pf = ensure_columns(pf, needed_columns['historico'], drop_extra_columns=False)

        print(pf)

        pf = pf.rename(columns={'dealId' : 'main_id', 'companyId' : 'company_id', 'pipelineStepId' : 'pipelineStep_id', 'pipelineId' : 'pipeline_id', 'userId' : 'user_Id'})
        
        deals_sql = pd.read_sql_query('SELECT main_id FROM deals', engine)
        
        correct_ids_deals = deals_sql['main_id'].to_numpy()
        correct_ids_df = df['main_id'].to_numpy()

        # Combine IDs from both sources (removes duplicates automatically)
        correct_ids_combined = np.union1d(correct_ids_deals, correct_ids_df)

        # Filter `pf` to keep only rows with `main_id` in either DataFrame
        pf = pf[pf['main_id'].isin(correct_ids_combined)]

        pf['enterDate'] = pd.to_datetime(pf['enterDate'], errors='coerce')
        pf['log_date'] = pf['enterDate'].dt.date

        log_operation("logs data extracted succesfully!", "success")
    except Exception as e:
        log_error_report(e)
        log_operation("logs data extraction failed!", "failed", str(e))

    # Load matriz_equipes from Excel
    try:
        matriz_equipes = pd.read_excel(r'./xlsx_files/matriz_equipes.xlsx', header=0)  # Ensure the first row is used as the header
        matriz_equipes.columns = ['equipe_id', 'equipe_name']  # Rename columns explicitly if needed

        # Ensure it is a DataFrame
        if not isinstance(matriz_equipes, pd.DataFrame):
            print("matriz_equipes is not a DataFrame. Converting to DataFrame.")
            matriz_equipes = pd.DataFrame(matriz_equipes)

        # Ensure the required columns exist
        required_columns = ['equipe_id', 'equipe_name']
        if not all(col in matriz_equipes.columns for col in required_columns):
            log_warning_report(f"Missing columns in matriz_equipes: {set(required_columns) - set(matriz_equipes.columns)}")
            # Create missing columns with default values
            for col in required_columns:
                if col not in matriz_equipes.columns:
                    matriz_equipes[col] = None  # Or some default value

        # Log the DataFrame structure for debugging
        print(f"matriz_equipes columns: {matriz_equipes.columns.tolist()}")
        print(f"matriz_equipes head:\n{matriz_equipes.head()}")
        print(type(matriz_equipes))
        log_operation("Succesfully loaded matriz_equipes", "success")
    except Exception as e:
        log_error_report(e)
        log_operation("Failed to load matriz_equipes", "failed", str(e))

    #excel script block
    try:
        matriz_executivos = pd.read_excel(r'./xlsx_files/matriz_executivos.xlsx')
        users = safe_merge(users, matriz_executivos, 'login', 'equipe_id', 'inner')
        
        del matriz_executivos
        log_operation("excel data fetched succesfully!", "success")
    except Exception as e:
        log_error_report(e)
        log_operation("excel data fetch failed!", "failed", str(e))

    #proposals script block
    try:
        gf = extract_adsim_data(proposals_url)

        matriz_geotargets = pd.read_excel(r'./xlsx_files/IDS_TargetsDigital.xlsx')        
        log_operation("proposals and geotargets dataframes created succesfully!", "success")
    except Exception as e:
        log_error_report(e)
        log_operation("proposals and geotargets dataframe creation failed!", "failed", str(e))

    #proposals_transforming script block
    try:
        gf = ensure_columns(gf, needed_columns['proposals'], drop_extra_columns=False)
        gf = gf.rename(columns={'id': 'proposal_id'})
        gf_deals = pd.json_normalize(gf['deal'])
        gf_deals = ensure_columns(gf_deals, needed_columns['gf_deals'], drop_extra_columns=False)
        gf['main_id'] = gf_deals['id']
        gf = drop_columns(gf, columns_to_drop=['deal'])

        items = gf.explode('items')[['main_id', 'proposal_id', 'items']]
        items = pd.json_normalize(items.to_dict(orient='records'))

        # **Error Handling and Debugging:**
        if items.empty:
          log_operation("items DataFrame is empty after json_normalize. Skipping column renaming.", "warning")
          # Continue with the code since it is not critical to stop here
        else:
            # Check if columns are strings before applying .str accessor:
            if isinstance(items.columns, pd.Index) and all(isinstance(col, str) for col in items.columns):
                items.columns = items.columns.str.replace('items.', '', regex=False)
                items.columns = items.columns.str.replace('.', '_', regex=False)
            else:
                log_operation(f"dues.columns is not a string-only index. Found type: {type(items.columns)}", "warning", f"Columns are {items.columns}")
                
            #if all columns are not string, then you need to convert:
            if not all(isinstance(col, str) for col in items.columns):
                new_columns = [str(col) for col in items.columns]
                items.columns = new_columns
                items.columns = items.columns.str.replace('items.', '', regex=False)
                items.columns = items.columns.str.replace('.', '_', regex=False)              
                log_operation("items.columns has been converted to string and replace operations made", "warning")        

        items = ensure_columns(items, needed_columns['items'], drop_extra_columns=False)

        gf = drop_columns(gf, columns_to_drop=['items'])
        
        items = items.rename(columns={'id' : 'item_id'})
        items = items.dropna(subset=['item_id'])
        items = drop_columns(items, columns_to_drop=['text', 'isText'])
        items = items.reset_index(drop=True)

        gf_executives = pd.json_normalize(gf['executive'])
        gf_executives = ensure_columns(gf_executives, expected_columns['gf_executives'])
        gf['executive_id'] = gf_executives['id']
        gf = drop_columns(gf, columns_to_drop=['executive'])

        gf['registerDate'] = pd.to_datetime(gf['registerDate'], errors='coerce')
        gf['lastUpdateDate'] = pd.to_datetime(gf['lastUpdateDate'], errors='coerce')
        gf['approvalDate'] = pd.to_datetime(gf['approvalDate'], errors='coerce')
        gf['rejectionDate'] = pd.to_datetime(gf['rejectionDate'], errors='coerce')

        gf['criacao_data'] = gf['registerDate'].dt.date
        gf['atualizacao_data'] = gf['lastUpdateDate'].dt.date
        gf['aprovacao_data'] = gf['approvalDate'].dt.date
        gf['rejeicao_data'] = gf['rejectionDate'].dt.date

        items_digital = gf.explode('itemsDigital')[['main_id', 'proposal_id', 'itemsDigital']]
        items_digital = pd.json_normalize(items_digital.to_dict(orient='records'))

        # **Error Handling and Debugging:**
        if items_digital.empty:
          log_operation("items DataFrame is empty after json_normalize. Skipping column renaming.", "warning")
          # Continue with the code since it is not critical to stop here
        else:
            # Check if columns are strings before applying .str accessor:
            if isinstance(items_digital.columns, pd.Index) and all(isinstance(col, str) for col in items_digital.columns):
                items_digital.columns = items_digital.columns.str.replace('itemsDigital.', '', regex=False)
                items_digital.columns = items_digital.columns.str.replace('.', '_', regex=False)
            else:
                log_operation(f"items_digital.columns is not a string-only index. Found type: {type(items_digital.columns)}", "warning", f"Columns are {items_digital.columns}")
                
            #if all columns are not string, then you need to convert:
            if not all(isinstance(col, str) for col in items_digital.columns):
                new_columns = [str(col) for col in items.columns]
                items_digital.columns = new_columns
                items_digital.columns = items_digital.columns.str.replace('itemsDigital.', '', regex=False)
                items_digital.columns = items_digital.columns.str.replace('.', '_', regex=False)           
                log_operation("items_digital.columns has been converted to string and replace operations made", "warning")

        items_digital = ensure_columns(items_digital, needed_columns['items_digital'], drop_extra_columns=False)

        items_digital = items_digital.rename(columns={'geotarget_name' : 'displayLocation_name', 'geotarget_initials' : 'displayLocation_initials', 'id' : 'item_id'})
        items_digital = items_digital.dropna(subset=['item_id'])
        items_digital = safe_merge(items_digital, matriz_geotargets, 'displayLocations_initials', 'displayLocation_id', 'left')
        items_digital = items_digital.reset_index(drop=True)
        gf = drop_columns(gf, columns_to_drop=['itemsDigital'])

        cha_cols = ['channel_id', 'channel_name', 'channel_initials']
        dis_cols = ['displayLocation_id', 'displayLocation_name', 'displayLocation_initials']
        prd_cols = ['product_id', 'product_name']
        for_cols = ['format_id', 'format_name', 'format_initials']

        channels = pd.concat([items[cha_cols], items_digital[cha_cols]], axis=0, ignore_index=True)
        displayLocations = pd.concat([items[dis_cols], matriz_geotargets[dis_cols]], axis=0, ignore_index=True)
        products2 = pd.concat([items[prd_cols], items_digital[prd_cols]], axis=0, ignore_index=True)
        programs = items[['program_id', 'program_name', 'program_initials']].copy()

        if not all(col in items.columns for col in for_cols) or not all(col in items_digital.columns for col in for_cols):
            formats = pd.DataFrame(columns=for_cols)
            formats = ensure_columns(formats, for_cols, drop_extra_columns=False)
        else: 
            formats = pd.concat([items[for_cols], items_digital[for_cols]], axis=0, ignore_index=True)

        drop_cols = ['channel_name', 'channel_initials', 'displayLocation_name', 'displayLocation_initials', 'format_name', 'format_initials', 'product_name']
        drop_cols1 = ['program_name', 'program_initials']
            
        items = drop_columns(items, columns_to_drop=drop_cols)
        items = drop_columns(items, columns_to_drop=drop_cols1)
        items_digital = drop_columns(items_digital, columns_to_drop=drop_cols)

        channels.loc[channels['channel_id'] == 1154, 'channel_id'] = 941
        channels.loc[channels['channel_id'] == 944, 'channel_id'] = 934
        channels.loc[channels['channel_id'] == 955, 'channel_id'] = 934

        items.loc[items['channel_id'] == 1154, 'channel_id'] = 941
        items.loc[items['channel_id'] == 944, 'channel_id'] = 934
        items.loc[items['channel_id'] == 955, 'channel_id'] = 934

        items_digital.loc[items_digital['channel_id'] == 1154, 'channel_id'] = 941
        items_digital.loc[items_digital['channel_id'] == 944, 'channel_id'] = 934
        items_digital.loc[items_digital['channel_id'] == 955, 'channel_id'] = 934

        channels = channels.drop_duplicates(subset=['channel_id'])
        displayLocations = displayLocations.drop_duplicates(subset=['displayLocation_id'])
        products = products.drop_duplicates(subset=['product_id'])
        programs = programs.drop_duplicates(subset='program_id')
        formats = formats.drop_duplicates(subset=['format_id'])

        channels = channels.dropna(subset=['channel_id'])
        displayLocations = displayLocations.dropna(subset=['displayLocation_id'])
        products = products.dropna(subset=['product_id'])
        programs = programs.dropna(subset=['program_id'])
        formats = formats.dropna(subset=['format_id'])

        items = drop_columns(items, columns_to_drop=['items'])
        items_digital = drop_columns(items_digital, columns_to_drop=['itemsDigital'])

        if items.empty and items_digital.empty:
            log_operation("Both items and items_digital are empty. Skipping concatenation.", "warning")
        else:
            items = pd.concat([items, items_digital], axis=0, ignore_index=True)
            if items_digital.empty:
                log_operation("items_digital was empty, keeping only items.", "warning")
            elif items.empty:
                log_operation("items was empty, using items_digital only.", "warning")
                items = items_digital.copy()
            else:
                log_operation("items and items_digital concatenated successfully.", "success")

        agencia_emails = agencia_emails.rename(columns={'agencia_id' : 'organization_id'})
        agencies = agencies.rename(columns={'agencia_id' : 'organization_id'})
        agencia_phoneNumbers = agencia_phoneNumbers.rename(columns={'agencia_id' : 'organization_id'})

        organization = pd.concat([agencies,organization], axis=0, ignore_index=True)
        organization_emails = pd.concat([agencia_emails,organization_emails], axis=0, ignore_index=True)
        organization_phoneNumbers = pd.concat([agencia_phoneNumbers,organization_phoneNumbers], axis=0, ignore_index=True)

        organization = organization.drop_duplicates(subset=['organization_id'])
        organization = organization.loc[:, ~organization.columns.duplicated()]   
        matriz_geotargets.head()

        del agencies, agencia_phoneNumbers, agencia_emails
        log_operation("proposal dataframe cleaned succesfully!", "success")
    except Exception as e:
        log_error_report(e)
        log_operation("proposal dataframe cleaning failed!", "failed", str(e))

    #activities script block
    try:
        activities = extract_adsim_data(activities_url)
        activities = ensure_columns(activities, needed_columns['activities'], drop_extra_columns=False)
        activities['startDate'] = pd.to_datetime(activities['startDate'])
        activities['endDate'] = pd.to_datetime(activities['endDate'])
        activities['doneDate'] = pd.to_datetime(activities['doneDate'])

        activities = activities.rename(columns={'userOwnerId' : 'user_id', 'dealId' : 'main_id'})

        try:
            activitiesOrg = pd.json_normalize(activities['organization'],sep='_')
            activitiesPers = pd.json_normalize(activities['person'],sep='_')
            activitiesComp = pd.json_normalize(activities['company'],sep='_')
        except Exception as e:
            log_error_report(e)
            activitiesOrg = pd.DataFrame()
            activitiesPers = pd.DataFrame()
            activitiesComp = pd.DataFrame()

        activitiesOrg = ensure_columns(activitiesOrg, needed_columns['activitiestemp'], drop_extra_columns=False)
        activitiesPers = ensure_columns(activitiesPers, needed_columns['activitiestemp'], drop_extra_columns=False)
        activitiesComp = ensure_columns(activitiesComp, needed_columns['activitiestemp'], drop_extra_columns=False)

        try:
            activities['organization_id'] = activitiesOrg['id']
            activities['person_id'] = activitiesPers['id']
            activities['company_id'] = activitiesComp['id']

            activity_type = pd.json_normalize(activities['type'],sep='_')

            activities['type_id'] = activity_type['id']

            activity_type = activity_type.drop_duplicates(subset=['id'])
            activity_type = activity_type.rename(columns={'id' : 'type_id'})
        except Exception as e:
            log_error_report(e)
            activity_type = pd.DataFrame()

        activity_type = ensure_columns(activity_type, needed_columns['activitiestemp'], drop_extra_columns=False)

        activities = activities.rename(columns={'id' : 'activity_id'})

        activities_checkin = pd.json_normalize(activities['checkin'],sep='_')
        activities_checkin = ensure_columns(activities_checkin, needed_columns['activities_checkin'], drop_extra_columns=False)
        activities_checkin['activity_id'] = activities['activity_id']

        activities_checkin = activities_checkin.dropna(subset=['date'])
        activities_checkin['date'] = pd.to_datetime(activities_checkin['date'])

        activities = safe_merge(activities, activities_checkin, 'activity_id', ['date', 'latitude', 'longitude'], 'left')

        activities = activities.rename(columns={'date': 'checkin_date', 'latitude' : 'activity_latitude', 'longitude' : 'activity_longitude'})

        activities = drop_columns(activities, ['type', 'organization', 'person', 'company', 'user', 'checkin'])
        activity_type = drop_columns(activity_type, ['isActive'])

        activities = activities[
            activities['main_id'].isin(correct_ids_combined) | 
            activities['main_id'].isna()
        ]

        del activities_checkin, activitiesComp, activitiesOrg, activitiesPers

        log_operation("activity and activity type dataframes created succesfully!", "success")

    except Exception as e:
        log_error_report(e)
        log_operation("activity and activity type dataframes creation failed!", "failed", str(e))

    #sales script block
    try:
        def fetch_sales_data(gc):
            """Function to fetch sales data from Google Sheets."""
            planilha = gc.open("VENDAS 2025 VERSÃO EUA")
            aba = planilha.worksheet("sheet")
            dados = aba.get_all_records()
            return pd.DataFrame(dados)

        gc = login()
        timeout_seconds = 35

        with futures.ThreadPoolExecutor() as executor:
            future = executor.submit(fetch_sales_data, gc)
            try:
                vendas = future.result(timeout=timeout_seconds)
                log_operation("sales dataframe created succesfully!", "success")
            except futures.TimeoutError:
                log_error_report(TimeoutError(f"Fetching sales data from Google Sheets timed out after {timeout_seconds} seconds."))
                log_operation("sales dataframe creation failed due to timeout!", "failed", f"Timeout after {timeout_seconds} seconds")
                vendas = pd.DataFrame() 
            except Exception as e:
                log_error_report(e)
                log_operation("sales dataframe creation failed!", "failed", str(e))
                vendas = pd.DataFrame() 
    except Exception as e:
        log_error_report(e)
        log_operation("sales dataframe creation failed!", "failed", str(e))
        vendas = pd.DataFrame() 

    #sales tranforming script block
    try:
        vendas = ensure_columns(vendas, needed_columns['sales'], drop_extra_columns=False)
        users['EXECUTIVO'] = users['name'] + ' ' + users['lastname']
        users['EXECUTIVO'] = users['EXECUTIVO'].str.upper()

        vendas.loc[vendas['EXECUTIVO'].str.contains('NOVOS'), 'EXECUTIVO'] = "GILSON BETTE"

        vendas.loc[(vendas['EXECUTIVO'].str.contains('GESTÃO')) & (vendas['PLATAFORMA'].str.contains('JP CURITIBA')), 'EXECUTIVO'] = "BRUNO MARFURTE"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('GESTÃO')) & (vendas['PLATAFORMA'].str.contains('JP CASCAVEL')), 'EXECUTIVO'] = "JOSIELI BASTIANI"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('GESTÃO')) & (vendas['PLATAFORMA'].str.contains('NEWS CURITIBA')), 'EXECUTIVO'] = "BRUNO MARFURTE"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('GESTÃO')) & (vendas['PLATAFORMA'].str.contains('TOPVIEW')), 'EXECUTIVO'] = "LEONARDO ZAIDAN"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('GESTÃO')) & (vendas['REGIÃO'].str.contains('MARINGÁ')), 'EXECUTIVO'] = "ANDRÉ MORAIS"
        vendas.loc[vendas['EXECUTIVO'].str.contains('GERÊNCIA FOZ'), 'EXECUTIVO'] = 'PEDRO ANDRADE'

        vendas.loc[(vendas['EXECUTIVO'].str.contains('CARTEIRA 3')) & (vendas['REGIÃO'].str.contains('PONTA GROSSA')), 'EXECUTIVO'] = "MATHEUS KONIG"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('CARTEIRA 4')) & (vendas['REGIÃO'].str.contains('PONTA GROSSA')), 'EXECUTIVO'] = "MATHEUS KONIG"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('CARTEIRA 6')) & (vendas['REGIÃO'].str.contains('MARINGÁ')), 'EXECUTIVO'] = "ANDRÉ MORAIS"   
        vendas.loc[(vendas['EXECUTIVO'].str.contains('EXECUTIVO 06')) & (vendas['REGIÃO'].str.contains('MARINGÁ')), 'EXECUTIVO'] = "ANDRÉ MORAIS" 

        vendas.loc[(vendas['EXECUTIVO'].str.contains('SEDE')) & (vendas['REGIÃO'].str.contains('CURITIBA')), 'EXECUTIVO'] = "ANDERSON SOUZA"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('SEDE')) & (vendas['REGIÃO'].str.contains('PONTA GROSSA')), 'EXECUTIVO'] = "MATHEUS KONIG"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('SEDE')) & (vendas['REGIÃO'].str.contains('MARINGÁ')), 'EXECUTIVO'] = "FABIO GOES"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('SEDE')) & (vendas['REGIÃO'].str.contains('OESTE')), 'EXECUTIVO'] = "PEDRO ANDRADE"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('SEDE')) & (vendas['REGIÃO'].str.contains('LONDRINA')), 'EXECUTIVO'] = "RODRIGO TABORDA"

        vendas.loc[(vendas['EXECUTIVO'].str.contains('PROJETO')) & (vendas['REGIÃO'].str.contains('CURITIBA')), 'EXECUTIVO'] = "ANDERSON SOUZA"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('PROJETO')) & (vendas['REGIÃO'].str.contains('PONTA GROSSA')), 'EXECUTIVO'] = "MATHEUS KONIG"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('PROJETO')) & (vendas['REGIÃO'].str.contains('LONDRINA')), 'EXECUTIVO'] = "RODRIGO TABORDA"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('PROJETO')) & (vendas['REGIÃO'].str.contains('OESTE')), 'EXECUTIVO'] = "PEDRO ANDRADE"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('PROJETO')) & (vendas['REGIÃO'].str.contains('MARINGÁ')), 'EXECUTIVO'] = "FABIO GOES"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('PROJETO')) & (vendas['REGIÃO'].str.contains('NACIONAL')), 'EXECUTIVO'] = "JOSÉ TRAVAGIN"

        vendas.loc[(vendas['EXECUTIVO'].str.contains('CONCESSIONÁRIO')) & (vendas['REGIÃO'].str.contains('CURITIBA')), 'EXECUTIVO'] = "ANDERSON SOUZA"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('CONCESSIONÁRIO')) & (vendas['REGIÃO'].str.contains('LONDRINA')), 'EXECUTIVO'] = "RODRIGO TABORDA"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('CONCESSIONÁRIO')) & (vendas['REGIÃO'].str.contains('OESTE')), 'EXECUTIVO'] = "PEDRO ANDRADE"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('CONCESSIONÁRIO')) & (vendas['REGIÃO'].str.contains('MARINGÁ')), 'EXECUTIVO'] = "FABIO GOES"

        vendas.loc[(vendas['EXECUTIVO'].str.contains('ENTRE PRAÇAS')) & (vendas['REGIÃO'].str.contains('CURITIBA')), 'EXECUTIVO'] = "ANDERSON SOUZA"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('ENTRE PRAÇAS')) & (vendas['REGIÃO'].str.contains('LONDRINA')), 'EXECUTIVO'] = "RODRIGO TABORDA"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('ENTRE PRAÇAS')) & (vendas['REGIÃO'].str.contains('MARINGÁ')), 'EXECUTIVO'] = "FABIO GOES"
        vendas.loc[(vendas['EXECUTIVO'].str.contains('ENTRE PRAÇAS')) & (vendas['REGIÃO'].str.contains('OESTE')), 'EXECUTIVO'] = "PEDRO ANDRADE"

        vendas = vendas[vendas['REGIÃO'] != 'ESP. CEDIDO']
        vendas = vendas[vendas['EXECUTIVO'] != 'PERFORMANCE']
        vendas = vendas[vendas['EXECUTIVO'] != 'AJUSTE DE META']
        vendas = vendas[vendas['PRAÇA'] != 'PROGRAMÁTICA']
        vendas = vendas[vendas['EXECUTIVO'] != 'PREFEITURA INTERIOR']
        vendas = vendas[vendas['ID POWER BI'] != '#REF!']

        users = users.drop_duplicates(subset=['user_id'])
        vendas['EXECUTIVO'] = vendas['EXECUTIVO'].str.strip()
        vendas = safe_merge(vendas, users, 'EXECUTIVO', 'user_id', 'left')

        users = drop_columns(users, columns_to_drop=['EXECUTIVO'])

        vendas = vendas.dropna(subset=['ID POWER BI'])

        channels.loc[channels['channel_id'] == 1154, 'channel_id'] = 941
        channels.loc[channels['channel_id'] == 484, 'channel_id'] = 941
        channels.loc[channels['channel_id'] == 944, 'channel_id'] = 934
        channels.loc[channels['channel_id'] == 955, 'channel_id'] = 934
        channels = channels.drop_duplicates(subset=['channel_id'])
        channels = channels.dropna(subset=['channel_id'])

        items.loc[items['channel_id'] == 1154, 'channel_id'] = 941
        items.loc[items['channel_id'] == 484, 'channel_id'] = 941
        items.loc[items['channel_id'] == 944, 'channel_id'] = 934
        items.loc[items['channel_id'] == 955, 'channel_id'] = 934

        dues.loc[dues['channel_id'] == 1154, 'channel_id'] = 941
        dues.loc[dues['channel_id'] == 944, 'channel_id'] = 934
        dues.loc[dues['channel_id'] == 955, 'channel_id'] = 934
        dues.loc[dues['channel_id'] == 484, 'channel_id'] = 941
        channels.loc[channels['channel_id'] == 941, 'channel_name'] = 'TOPVIEW'

        vendas.loc[vendas['PRAÇA'].str.contains('INSTITUC.'), 'PRAÇA'] = 'INSTITUCIONAL'
        vendas.loc[vendas['user_id'] == 24436, 'PRAÇA'] = 'INSTITUCIONAL'
        vendas.loc[vendas['PLATAFORMA'].str.contains('GOV'), 'PLATAFORMA'] = vendas.loc[vendas['PLATAFORMA'].str.contains('GOV'), 'FONTE DE DADOS'].values
        vendas.loc[vendas['PLATAFORMA'].str.contains('WTC'), 'PLATAFORMA'] = 'DIGITAL'
        vendas.loc[vendas['PLATAFORMA'].str.contains('RIC PODCAST'), 'PLATAFORMA'] = 'DIGITAL'
        vendas.loc[vendas['PLATAFORMA'].str.contains('RIC LAB'), 'PLATAFORMA'] = 'DIGITAL'
        vendas.loc[vendas['PLATAFORMA'].str.contains('JOY'), 'PLATAFORMA'] = 'JP CURITIBA'
        vendas.loc[vendas['PLATAFORMA'].str.contains('TV'), 'PLATAFORMA'] = 'RICTV RECORD'
        vendas.loc[vendas['PLATAFORMA'].str.contains('RÁDIO'), 'PLATAFORMA'] = 'JOVEM PAN PR'
        vendas.loc[vendas['PLATAFORMA'].str.contains('REVISTA'), 'PLATAFORMA'] = 'TOPVIEW'
        vendas.loc[vendas['PLATAFORMA'].str.contains('RICtv'), 'PLATAFORMA'] = 'RICTV RECORD'
        vendas.loc[vendas['PLATAFORMA'].str.contains('JP'), 'PLATAFORMA'] = 'JOVEM PAN PR'
        vendas.loc[vendas['PLATAFORMA'].str.contains('NEWS'), 'PLATAFORMA'] = 'JOVEM PAN NEWS PR'
        vendas.loc[vendas['PLATAFORMA'].str.contains('DIGITAL'), 'PLATAFORMA'] = 'PORTAL ric.com.br'

        vendas['PLATAFORMA'] = vendas['PLATAFORMA'].str.strip()
        channels['channel_name'] = channels['channel_name'].str.strip()
        
        vendas = vendas.rename(columns={'PLATAFORMA' : 'channel_name', 'PRAÇA' : 'title'})

        vendas = safe_merge(vendas, channels, id_column='channel_name', columns_to_merge=['channel_id'], merge_type='left')
        vendas = safe_merge(vendas, pipeline, id_column='title', columns_to_merge=['pipeline_id'], merge_type='left')

        vendas = drop_columns(vendas, columns_to_drop=['HISTÓRICO 2024', 'VIRADA', 'MÊS ANTERIOR', 'MÊS ATUAL X MÊS ANTERIOR', 
                                    'CRESCIMENTO 2025X2024', 'channel_name', 'title', 'EXECUTIVO', 'PREMIAÇÃO DIRETORIA GERAL', 'PREMIAÇÃO DIRETORIA DE PRAÇA', 
                                            'PREMIAÇÃO DIRETORIA DE PRAÇA', 'PREMIAÇÃO DIRETORIA NACIONAL', 'PREMIAÇÃO GESTOR DIGITAL', 'PREMIAÇÃO INSTITUCIONAL', 'PREMIAÇÃO GERÊNCIA',
                                            'PREMIAÇÃO INDIVIDUAL', 'PREMIAÇÃO HEAD DIGITAL', 'FORECAST 1', 'FORECAST 2'])

        df.loc[(df['pipeline_id'] == 1233) & (df['pipelineStep_id'] == 6865), 'isWon'] = True

        vendas['META'] = vendas['META'].fillna(0)
        vendas['REALIZADO'] = vendas['REALIZADO'].fillna(0)

        log_operation("sales dataframe transformed succesfully!", "success")
    except Exception as e:
        log_error_report(e)
        log_operation("sales dataframe transformation failed!", "failed", str(e))

    #normalization script block
    try:

        #this step will ensure that the expected columns from the database are in the dataframe, if a column is not necessary, it'll drop it.
        users = ensure_columns(users, expected_columns['users'], drop_extra_columns=True)
        pipeline = ensure_columns(pipeline, expected_columns['pipeline'], drop_extra_columns=True)
        segments = ensure_columns(segments, expected_columns['segments'], drop_extra_columns=True)
        portfolios = ensure_columns(portfolios, expected_columns['portfolios'], drop_extra_columns=True)
        pipelineStep = ensure_columns(pipelineStep, expected_columns['pipelineStep'], drop_extra_columns=True)
        company = ensure_columns(company, expected_columns['company'], drop_extra_columns=True)
        dealType = ensure_columns(dealType, expected_columns['dealType'], drop_extra_columns=True)
        dues = ensure_columns(dues, expected_columns['dues'], drop_extra_columns=True)
        person = ensure_columns(person, expected_columns['person'], drop_extra_columns=True)
        pf = ensure_columns(pf, expected_columns['historico'], drop_extra_columns=True)
        products = ensure_columns(products, expected_columns['products'], drop_extra_columns=True)
        df = ensure_columns(df, expected_columns['deals'], drop_extra_columns=True)
        organization = ensure_columns(organization, expected_columns['organization'], drop_extra_columns=True)
        channels = ensure_columns(channels, expected_columns['channels'], drop_extra_columns=True)
        formats = ensure_columns(formats, expected_columns['formats'], drop_extra_columns=True)
        displayLocations = ensure_columns(displayLocations, expected_columns['displayLocations'], drop_extra_columns=True)
        gf = ensure_columns(gf, expected_columns['proposals'], drop_extra_columns=True)
        items = ensure_columns(items, expected_columns['items'], drop_extra_columns=True)
        activities = ensure_columns(activities, expected_columns['activities'], drop_extra_columns=True)
        activity_type = ensure_columns(activity_type, expected_columns['activity_type'], drop_extra_columns=True)

        #since we're fetching data from an api, it's best to ensure that values are what the database expects
        df = convert_columns_to_int(df, ['main_id', 'organization_id', 'dealType_id', 'person_id', 'agencies_id', 'products_id', 'pipeline_id', 
                                         'pipelineStep_id', 'creatorUser_id', 'responsible_id', 'company_id', 'activitiesQuantity', 'productsQuantity', 'sequenceOrder', ''])
        users = convert_columns_to_int(users, ['user_id', 'equipe_id'])
        organization = convert_columns_to_int(organization, ['organization_id', 'company_id', 'segment_id', 'portfolio_id'])
        dues = convert_columns_to_int(dues, ['dues_id', 'user_id', 'company_id', 'displayLocation_id', 'dealProposalItemId', 'channel_id', 'product_id'])
        portfolios = convert_columns_to_int(portfolios, ['companyId', 'user_id'])
        products = convert_columns_to_int(products,['product_id'])
        vendas = convert_columns_to_int(vendas, ['user_id', 'channel_id', 'pipeline_id'])
        pf = convert_columns_to_int(pf, ['main_id', 'id', 'pipeline_id', 'pipelineStep_id', 'company_id'])
        channels = convert_columns_to_int(channels, ['channel_id'])
        displayLocations = convert_columns_to_int(displayLocations, ['displayLocation_id'])
        formats = convert_columns_to_int(formats, ['format_id'])
        pipeline = convert_columns_to_int(pipeline, ['pipeline_id'])
        pipelineStep = convert_columns_to_int(pipelineStep, ['pipelineStep_id'])
        person = convert_columns_to_int(person, ['person_id'])
        dealType = convert_columns_to_int(dealType, ['dealType_id'])
        gf = convert_columns_to_int(gf, ['proposal_id', 'main_id', 'executive_id', 'version'])
        items = convert_columns_to_int(items, ['item_id', 'product_id', 'channel_id', 'main_id', 'groupidentifier', 'product_id', 'quantitytotal', 'channel_id', 'displaylocation_id', 'format_id', 'program_id'])
        activities = convert_columns_to_int(activities, ['main_id', 'activity_id', 'user_id', 'company_id', 'organization_id', 'person_id', 'type_id'])
        activity_type = convert_columns_to_int(activity_type, ['type_id'])

        #replacing some errors with none
        gf = gf.replace({np.nan : None})
        items = items.replace({np.nan : None})
        dues = dues.replace({np.nan : None})
        organization = organization.replace({np.nan : None})
        df = df.replace({pd.NA: None, pd.NaT : None})
        dues = dues.replace({pd.NA : None, pd.NaT : None})
        portfolios = portfolios.replace({pd.NA : None})
        organization = organization.replace({pd.NA: None, pd.NaT : None})        
        vendas = vendas.replace({pd.NA : None})
        vendas = vendas.replace({'' : None})
        activities = activities.replace({pd.NA : None})
        activity_type = activity_type.replace({pd.NA : None})
        organization.loc[organization['isAgency'] == None, 'isAgency'] = False
        organization.loc[organization['municipalRegistration'] == None, 'municipalRegistration'] = False
        organization.loc[organization['stateRegistration'] == None, 'stateRegistration'] = False

        dues.loc[dues['netValue'] == None, 'netValue'] = 0
        dues.loc[dues['value'] == None, 'value'] = 0   

        #changing dataframe column names to lower case
        df.columns = df.columns.str.lower()
        users.columns = users.columns.str.lower()
        pipeline.columns = pipeline.columns.str.lower()
        pipelineStep.columns = pipelineStep.columns.str.lower()
        organization.columns = organization.columns.str.lower()
        organization_emails.columns = organization_emails.columns.str.lower()
        organization_phoneNumbers.columns = organization_phoneNumbers.columns.str.lower()
        dealType.columns = dealType.columns.str.lower()
        dues.columns = dues.columns.str.lower()
        company.columns = company.columns.str.lower()
        products.columns = products.columns.str.lower()
        segments.columns = segments.columns.str.lower()
        person.columns = person.columns.str.lower()
        pf.columns = pf.columns.str.lower()
        matriz_equipes.columns = matriz_equipes.columns.str.lower()
        gf.columns = gf.columns.str.lower()
        items.columns = items.columns.str.lower()
        channels.columns = channels.columns.str.lower()
        displayLocations.columns = displayLocations.columns.str.lower()
        programs.columns = programs.columns.str.lower()
        formats.columns = formats.columns.str.lower()
        portfolios.columns = portfolios.columns.str.lower()
        activities.columns = activities.columns.str.lower()
        activity_type.columns = activity_type.columns.str.lower()

        df = df.rename(columns={'products_id' : 'product_id',
                                'productsquantity' : 'productquantity'})
        
        pf = drop_columns(pf, columns_to_drop=['user_id'])

        vendas = vendas.rename(columns={'REGIÃO' : 'regiao',
                                        'AREA DE NEGÓCIO' : 'area_negocio',
                                        'MÊS/ANO' : 'mes_ano',
                                        'FONTE DE DADOS' : 'fonte_dados',
                                        'NEGÓCIO' : 'negocio',
                                        'ID POWER BI' : 'ID'})

        pipelineStep = pipelineStep.rename(columns={'pipelinestepid' : 'pipelinestep_id'})
        portfolios = portfolios.rename(columns={'companyid' : 'company_id'})

        vendas.columns = vendas.columns.str.lower()        

        vendas.loc[vendas['meta'] == None, 'meta'] = 0
        vendas.loc[vendas['realizado'] == None, 'realizado'] = 0
        vendas.loc[vendas['meta'] == "#REF!", 'meta'] = 0
        vendas.loc[vendas['realizado'] == "#REF!", 'realizado'] = 0
        
        channels.loc[channels['channel_id'] == 934, 'channel_name'] = 'DIGITAL'
        channels.loc[channels['channel_id'] == 934, 'channel_initials'] = 'RCD'

        df.loc[df['islost'] == True, 'pipelinestep_id'] = 61124
        df.loc[df['islost'] == True, 'sequenceorder'] = 7

        vendas = vendas.dropna(subset=['id'])

        log_operation("dataframe normalized succesfully!", "success")
    except Exception as e:
        log_error_report(e)
        log_operation("dataframe normalization failed!", "failed", str(e))

    try:
        # Establish connection
        conn = psycopg2.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password
        )
        print("Connected to the database!")
        
        # Create a cursor
        cursor = conn.cursor()
        
        # New Feature: Sync users from Google Sheet, checking if there's any new user not present in DB (public.users)
        try:
            log_operation("Starting user sync from Google Sheet.", "info")
            
            # 1. Login to Google Sheets
            gc = login()
            
            # 2. Fetch data from Google Sheet
            spreadsheet_url = "https://docs.google.com/spreadsheets/d/1W_4JFszD1hfYjD9P-3oNL02a10kK-nMCR2ELNptR1Vc/edit?gid=0#gid=0"
            sheet = gc.open_by_url(spreadsheet_url)
            worksheet = sheet.get_worksheet(0)  # Getting first sheet
            sheet_data = worksheet.get_all_records()
            sheet_users_df = pd.DataFrame(sheet_data)

            # Cleaning to get only users with user_id
            sheet_users_df.dropna(subset=['user_id'], inplace=True)
            sheet_users_df = sheet_users_df[sheet_users_df['user_id'] != '']
            
            # Ensure sheet dataframe has the right columns and types
            sheet_users_df = ensure_columns(sheet_users_df, expected_columns['users'], drop_extra_columns=True)
            sheet_users_df = convert_columns_to_int(sheet_users_df, ['user_id', 'equipe_id'])
            sheet_users_df.columns = sheet_users_df.columns.str.lower()

            log_operation(f"Successfully fetched {len(sheet_users_df)} users from Google Sheet.", "success")

            # 3. Fetch existing user IDs from the database
            db_users_df = pd.read_sql_query("SELECT user_id FROM public.users", engine)
            
            # 4. Identify new users
            if not db_users_df.empty:
                existing_user_ids = set(db_users_df['user_id'])
                new_users_df = sheet_users_df[~sheet_users_df['user_id'].isin(existing_user_ids)].copy()
            else:
                new_users_df = sheet_users_df.copy()

            # 5. Insert new users into the database
            if not new_users_df.empty:
                log_operation(f"Found {len(new_users_df)} new users to insert from Google Sheet.", "info")
                empty_update_df = pd.DataFrame(columns=new_users_df.columns)
                user_columns_to_check = [col for col in expected_columns['users'] if col != 'user_id']
                
                update_or_insert_rows(conn, cursor, "users", "user_id", user_columns_to_check, empty_update_df, new_users_df)
                log_operation(f"Finished inserting new users from Google Sheet.", "success")
            else:
                log_operation("No new users to insert from Google Sheet.", "info")

        except Exception as e:
            log_error_report(e)
            log_operation("User sync from Google Sheet failed.", "failed", str(e))
        # End of New Block

        table_mappings = {
        "teams": ("equipe_id", ['equipe_name'], matriz_equipes),
        "company": ("company_id", ['name', 'cnpjcpf'], company),
        "displaylocations": ("displaylocation_id", ['displaylocation_name', 'displaylocation_initials'], displayLocations),
        "channels" : ("channel_id", ['channel_name', 'channel_initials'], channels),
        "formats" : ("format_id", ['format_name', 'format_initials'], formats),
        "programs" : ("program_id", ['program_name', 'program_initials'], programs),
        "segments" : ("segment_id", ['isactive', 'isdeleted', 'description'], segments), 
        "users" : ("user_id", ['cpf', 'name', 'login', 'lastname', 'equipe_id'], users),
        "pipeline" : ("pipeline_id", ['title', 'isactive', 'isdeleted'], pipeline),
        "pipelinestep" : ("pipelinestep_id", ['title', 'goaldeal', 'isactive', 'goalvalue', 'isdeleted', 'isplanning', 'sequenceorder'], pipelineStep),
        "products" : ("product_id", ['product_name'], products),
        "organization" : ("organization_id", ['cpf', 'cnpj', 'name', 'isagency', 'registerdate', 
            'corporatename', 'stateregistration', 'municipalregistration', 
            'company_id', 'segment_id', 'portfolio_id'], organization),
        "dealtype" : ("dealtype_id", ['isactive', 'description'], dealType),
        "person" : ("person_id", ['cpf', 'name'], person),
        "portfolio" : ('portfolio_id', ['user_id', 'description', 'enddate', 'isactive', 'startdate', 'lastupdatedate'], portfolios),
        "deals" : ("main_id", ['pipeline_id', 'creatoruser_id', 'responsible_id', 'pipelinestep_id', 'organization_id', 
            'product_id', 'dealtype_id', 'agencies_id', 'iswon', 'islost', 'enddate', 'windate', 'losedate', 'netvalue', 
            'isdeleted', 'ispending', 'startdate', 'shelvedate', 'description', 'approvaldate', 'registerdate', 'sequenceorder', 
            'conclusiondate', 'conversiondate', 'lastupdatedate', 'negotiatedvalue', 'productquantity', 'forecastsalesdate', 'isadvancedproduct', 
            'activitiesquantity', 'hasproductswithquotas', 'agencycommissionpercentage'], df),
        "dues" : ("dues_id", ['main_id', 'value', 'user_id', 'channel_id', 'duedate', 
            'netvalue', 'company_id', 'paymentdate', 'registerdate', 'lastupdatedate', 'displaylocation_id'], dues),
        "sales" : ("id", ['regiao', 'area_negocio', 'produto', 'meta', 'realizado', 
            'porcentagem', 'mes_ano', 'origem', 'negocio', 'fonte_dados', 'user_id', 'channel_id', 'pipeline_id'], vendas),
        "historico" : ("id", ['enterdate', 'pipeline_id', 'pipelinestep_id', 'company_id', 'value', 'main_id'], pf),
        "proposals" : ("proposal_id", ['registerdate', 'lastupdatedate', 'isactive', 'version', 'isapproved', 'isrejected', 'notes', 'isapprovalrequested', 'tablevalue', 'averagediscountpercentage', 
                                       'negotiatedvalue', 'netvalue', 'discountpercentage', 'approvaldate', 'description', 'title', 'rejectiondate', 'rejectionreason', 'main_id', 'executive_id'], gf),
        "proposal_items" : ("item_id", ['proposal_id', 'product_id', 'channel_id', 'displaylocation_id', 'program_id', 'format_id', 'isgroupingproduct', 'iswithoutdelivery', 'groupidentifier',
                                        'unitaryvalue', 'tablevalue', 'quantitytotal', 'discountpercentage', 'negotiatedvalue', 'quantity', 'productioncostvalue', 'isproductioncosttodefine',
                                        'grossvalue', 'netvalue', 'isreapplication', 'distributiontype', 'startdate', 'enddate', 'durationseconds', 'issendtogoogleadmanager', 'issponsorship',
                                        'website_name', 'website_initials', 'device_name', 'page_name', 'visibility_name', 'nettablevalue', 'costmethod_name', 'costmethod_externalcode', 'costmethod_calculationstrategy',
                                        'totaltablevalue', 'main_id', 'producttouse_id', 'producttouse_name'], items),
        "activity_type" : ("type_id", ['description'], activity_type),
        "activities" : ("activity_id", ['main_id', 'organization_id', 'person_id', 'company_id', 'user_id', 'startdate', 'enddate', 'donedate', 'isdone', 
                                        'isallday', 'title', 'notes', 'checkin_date', 'checkin_latitude', 'checkin_longitude', 'type_id'], activities)
        }
        
        # New function to compare database with google sheet and delete missing rows
        def delete_missing_rows_from_db(conn, cursor, table_name, id_column, new_ids):
            """
            Deletes rows from the database table that are not present in the new_ids list.
            This is used to ensure the database matches the source of truth (e.g., Google Sheet).
            """
            db_ids = pd.read_sql_query(f"SELECT {id_column} FROM {table_name}", conn)[id_column]
            db_ids_set = set(db_ids)
            new_ids_set = set(new_ids)
            ids_to_delete = db_ids_set - new_ids_set
            if ids_to_delete:
                placeholders = ', '.join(['%s'] * len(ids_to_delete))
                delete_sql = f"DELETE FROM {table_name} WHERE {id_column} IN ({placeholders})"
                cursor.execute(delete_sql, tuple(ids_to_delete))
                conn.commit()
                log_operation(f"Deleted {len(ids_to_delete)} rows from {table_name} that were removed from the Google Sheet.", "success")

        for table_name, (id_column, columns_to_check, new_data_df) in table_mappings.items():
            try:
                # For the sales table, fetch all rows to ensure deletions work correctly
                if table_name == "sales":
                    sql_data = pd.read_sql_query(f"SELECT * FROM {table_name}", engine)
                    # Ensure DB matches sheet: delete any sales not present in the latest sheet
                    if not new_data_df.empty and id_column in new_data_df.columns:
                        new_ids = new_data_df[id_column].dropna().unique().tolist()
                        delete_missing_rows_from_db(conn, cursor, table_name, id_column, new_ids)
                else:
                    # Existing logic for other tables
                    if not new_data_df.empty and id_column in new_data_df.columns:
                        ids_to_fetch = new_data_df[id_column].dropna().unique().tolist()
                        if ids_to_fetch:
                            placeholders = ', '.join(['%s'] * len(ids_to_fetch))
                            sql_query = f"SELECT * FROM {table_name} WHERE {id_column} IN ({placeholders})"
                            sql_data = pd.read_sql_query(sql_query, engine, params=tuple(ids_to_fetch))
                            log_operation(f"Fetched {len(sql_data)} specific rows from {table_name} based on new data IDs.", "success")
                        else:
                            log_operation(f"No valid IDs found in new data for {table_name}. Fetching empty structure.", "info")
                            sql_data = pd.read_sql_query(f"SELECT * FROM {table_name} LIMIT 0", engine)
                    else:
                        log_operation(f"New data for {table_name} is empty or missing ID column '{id_column}'. Fetching empty structure.", "info")
                        sql_data = pd.read_sql_query(f"SELECT * FROM {table_name} LIMIT 0", engine)

                # Proceed with comparison and update
                compare_and_update_table(cursor, conn, table_name, id_column, columns_to_check, sql_data, new_data_df)
                time.sleep(2) 

            except Exception as e:
                log_error_report(e)
                log_operation(f"Error processing table {table_name}.", "failed", str(e))

        try:
            # Start a single transaction
            with conn:
                cursor.execute("""
                    -- Update 1: From displaylocations
                    UPDATE dues
                    SET channel_id = dl.channel_id
                    FROM displaylocations dl
                    WHERE dues.displaylocation_id = dl.displaylocation_id
                    AND dues.channel_id IS NULL;
                    
                    -- Update 2: From deals
                    UPDATE dues
                    SET user_id = deals.responsible_id
                    FROM deals
                    WHERE dues.main_id = deals.main_id
                    AND dues.user_id IS NULL;
                    
                    -- Update 3: From products
                    UPDATE dues
                    SET channel_id = p.channel_id
                    FROM deals d
                    JOIN products p ON d.product_id = p.product_id
                    WHERE dues.main_id = d.main_id
                    AND dues.channel_id IS NULL;
                    
                    -- Update 4: From proposal_items (exact match)
                    UPDATE dues d
                    SET channel_id = pi.channel_id
                    FROM (
                        SELECT main_id, channel_id, SUM(netvalue) AS total_net_value
                        FROM proposal_items
                        WHERE isgroupingproduct = false AND channel_id IS NOT NULL
                        GROUP BY main_id, channel_id
                    ) pi
                    WHERE d.main_id = pi.main_id
                    AND d.netvalue = pi.total_net_value
                    AND d.channel_id IS NULL;
                    
                    -- Update 5: From proposal_items (fallback)
                    UPDATE dues d
                    SET channel_id = p.channel_id
                    FROM proposal_items pi2
                    JOIN products p ON pi2.product_id = p.product_id
                    WHERE d.main_id = pi2.main_id
                    AND d.channel_id IS NULL
                    AND pi2.isgroupingproduct = false
                    AND p.channel_id IS NOT NULL;
                    
                    -- Update 6: Delete duplicates (dues)
                    WITH ranked_duplicates AS (
                        SELECT *,
                            ROW_NUMBER() OVER (
                                PARTITION BY channel_id, value, duedate, netvalue, paymentdate
                                ORDER BY registerdate DESC
                            ) AS rn
                        FROM dues
                    )

                    DELETE FROM dues
                    WHERE dues_id IN (
                        SELECT dues_id
                        FROM ranked_duplicates
                        WHERE rn > 1
                    );
                                
                    DELETE FROM basket_teste;
                               
                    -- Update 7: Delete Old Dues
                    WITH ranked_dues AS (
                        SELECT *,
                            ROW_NUMBER() OVER (PARTITION BY main_id ORDER BY registerdate DESC) AS rn
                        FROM dues
                        WHERE main_id IN (
                            SELECT a.main_id
                            FROM (
                                SELECT main_id, SUM(netvalue) AS total_netvalue
                                FROM dues
                                GROUP BY main_id
                            ) AS a
                            JOIN deals AS b ON a.main_id = b.main_id
                            WHERE (a.total_netvalue - b.netvalue) >= 1
                            AND b.iswon = true
                            AND b.netvalue <> 0
                        )
                    )
                    DELETE FROM dues
                    WHERE (main_id, registerdate) IN (
                        SELECT main_id, registerdate
                        FROM ranked_dues
                        WHERE rn > 1  -- Only delete if there are older records
                    );
                """)
            log_operation("All dues updates completed in single transaction", "success")

        except Exception as e:
            log_operation("Failed to update dues!", "failed", str(e))
            log_error_report(e)

        try:
            items = pd.read_sql_query("SELECT * FROM proposal_items", engine)
            dues = pd.read_sql_query("SELECT * FROM dues", engine)

            #Calculo novo de basket
            def calculo_novo(dues, items):
                soma_veiculo_e_praca = items.groupby(['main_id'], as_index=False).agg(
                    total_proposal_net_value=('netvalue', 'sum')
                )

                porcentagem = items.drop(['program_id', 'format_id', 'isgroupingproduct', 'iswithoutdelivery', 
                                        'groupidentifier', 'unitaryvalue', 'tablevalue', 'quantitytotal',
                                        'discountpercentage', 'negotiatedvalue', 'quantity', 'productioncostvalue',
                                        'isproductioncosttodefine', 'grossvalue', 'isreapplication', 'distributiontype', 'startdate',
                                        'enddate', 'durationseconds', 'issendtogoogleadmanager', 'issponsorship',
                                        'website_name', 'website_initials', 'device_name', 'page_name', 'visibility_name',
                                        'nettablevalue', 'costmethod_name', 'costmethod_externalcode', 'costmethod_calculationstrategy',
                                        'totaltablevalue', 'producttouse_id', 'producttouse_name', 'product_id'], axis=1)
                
                porcentagem = porcentagem.groupby(['main_id', 'channel_id', 'displaylocation_id'], as_index=False).agg(
                    channel_netvalue = ('netvalue', 'sum')
                )
                
                porcentagem = pd.merge(porcentagem, soma_veiculo_e_praca, how='left', on='main_id')

                porcentagem['porcentagem'] = porcentagem['channel_netvalue'] / porcentagem['total_proposal_net_value']

                dues = dues[dues['main_id'].isin(porcentagem['main_id'])]

                dues_months = dues.drop(['dues_id', 'company_id', 'value', 'paymentdate', 'registerdate', 'lastupdatedate', 'dealproposalitemid', 'channel_id', 'displaylocation_id'], axis=1)

                basket_teste = pd.merge(dues_months, porcentagem, how='left', on='main_id')

                basket_teste = basket_teste.drop(['channel_netvalue', 'total_proposal_net_value'], axis=1)

                basket_teste['basket_value'] = (
                    basket_teste['netvalue'] * basket_teste['porcentagem'].fillna(1)
                )

                basket_teste['channel_id'] = basket_teste['channel_id'].astype(int)
                basket_teste['displaylocation_id'] = basket_teste['displaylocation_id'].astype(int)

                basket_teste['basket_id'] = (
                    basket_teste['main_id'].astype(str) + 
                    basket_teste['user_id'].astype(str) + 
                    basket_teste['channel_id'].astype(str) + 
                    basket_teste['displaylocation_id'].astype(str) + 
                    basket_teste['duedate'].astype(str)
                )

                basket_teste = basket_teste.drop(['porcentagem', 'netvalue'], axis=1)

                return basket_teste

            df_teste = calculo_novo(dues, items)

            # Insert into PostgreSQL
            df_teste.to_sql(
                'basket_teste',        # Table name
                engine,                # SQLAlchemy engine
                if_exists='replace',   # 'append', 'replace', or 'fail'
                index=False            # Don't write DataFrame index
            )

            log_operation("Inserted basket_teste", "success")
        except Exception as e:
            log_operation("Failed to update basket_teste!", "failed", str(e))
            log_error_report(e)

        # Close connection
        cursor.close()
        conn.close()
    except Exception as e:
        conn.rollback()
        print("Error connecting to the database:", e)
    
    save_report(report)

if __name__ == "__main__":
    main()