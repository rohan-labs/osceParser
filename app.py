import os
import json
import streamlit as st
import openai as OpenAI
import supabase as Client
from supabase import create_client
import io as StringIO
import tempfile as NamedTemporaryFile
import time
import PyPDF2
import docx2txt
from dotenv import load_dotenv

def get_env_variable(var_name):
  try:
      return st.secrets[var_name]
  except Exception:
      return os.getenv(var_name)

openai_api_key = get_env_variable("OPENAI_API_KEY")
supabase_url = get_env_variable("SUPABASE_URL")
supabase_key = get_env_variable("SUPABASE_KEY")
assistant_id = get_env_variable("ASSISTANT_ID")

if not openai_api_key or not supabase_url or not supabase_key or not assistant_id:
    st.error("API keys, credentials, or assistant ID are not properly set.")
    st.stop()

client = OpenAI(api_key=openai_api_key)

# Retrieve Assistant
st.session_state.assistant = client.beta.assistants.retrieve(assistant_id)

# Session state for chat
if "messages" not in st.session_state:
    st.session_state.messages = []

# Display chat messages
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# Chat input
if prompt := st.chat_input("What is your question?"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Create a new thread for each conversation
    thread = client.beta.threads.create()

    # Add user message to the thread
    client.beta.threads.messages.create(
        thread_id=thread.id,
        role="user",
        content=prompt
    )

    # Run the assistant
    run = client.beta.threads.runs.create(
        thread_id=thread.id,
        assistant_id=st.session_state.assistant.id,
    )

    # Wait for the run to complete
    while run.status != "completed":
        run = client.beta.threads.runs.retrieve(thread_id=thread.id, run_id=run.id)

    # Retrieve and display the assistant's response
    messages = client.beta.threads.messages.list(thread_id=thread.id)
    assistant_message = messages.data[0].content[0].text.value

    st.session_state.messages.append({"role": "assistant", "content": assistant_message})
    with st.chat_message("assistant"):
        st.markdown(assistant_message)

# Initialise Supabase client
supabase: Client = create_client(supabase_url, supabase_key)

# Streamlit app
st.title("OSCE Station Uploader and Parser")

st.write("""
This app allows you to upload multiple PDF, DOCX, or TXT files.
It will parse the content via the OpenAI API and convert it into the JSON
format required for the staticOSCE table, then upload it to Supabase.
""")

uploaded_files = st.file_uploader(
    "Upload PDF, DOCX, or TXT files",
    type=["pdf", "docx", "txt"],
    accept_multiple_files=True
)

if uploaded_files:
    data_list = []
    any_errors = False

    for uploaded_file in uploaded_files:
        file_name = uploaded_file.name
        st.write(f"Processing **{file_name}**...")

        # Read the file content based on its type
        try:
            if uploaded_file.type == "application/pdf":
                with NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
                    temp_pdf.write(uploaded_file.read())
                    temp_pdf.flush()
                    reader = PyPDF2.PdfReader(temp_pdf.name)
                    text_content = ""
                    for page in reader.pages:
                        text_content += page.extract_text()
            elif uploaded_file.type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                with NamedTemporaryFile(delete=False, suffix=".docx") as temp_docx:
                    temp_docx.write(uploaded_file.read())
                    temp_docx.flush()
                    text_content = docx2txt.process(temp_docx.name)
            elif uploaded_file.type == "text/plain":
                stringio = StringIO(uploaded_file.getvalue().decode("utf-8"))
                text_content = stringio.read()
            else:
                st.error(f"Unsupported file type: {uploaded_file.type}")
                continue
        except Exception as e:
            st.error(f"Error reading {file_name}: {e}")
            any_errors = True
            continue

        # Use OpenAI API to parse the content
        max_retries = 3
        retry_delay = 5

        for attempt in range(max_retries):
            try:
                full_text_content = text_content

                # Craft prompt for OSCE station data
                prompt = f"""
You are provided text describing one or more OSCE stations. You must extract and parse
the following fields for each station:

- actorBrief
- examinerBrief
- markscheme
- category
- stationName
- candidateBrief

Each of these must be treated as a string and should retain every word, including markdown or quotes.

You MUST ensure you do not summarise or omit any detail. You must include every aspect, paragraph, and nuance from the text. 
If there are multiple stations, each should be numbered and output separately in a JSON object (like 0, 1, 2, etc.).

For example:

actorBrief: The actor is a 50-year-old father of three. He complains of acute onset breathlessness...
examinerBrief: Please observe how the candidate addresses issues of acute confusion...
markscheme: 1 mark for checking the patient's alertness. 1 mark for administering oxygen...
category: Respiratory
stationName: Acute Respiratory Distress
candidateBrief: You are a junior doctor in A&E. A 50-year-old man presents with sudden respiratory distress...

The output format should look like this:

{{
  "0": {{
    "actorBrief": "...",
    "examinerBrief": "...",
    "markscheme": "...",
    "category": "...",
    "stationName": "...",
    "candidateBrief": "..."
  }}
}}

Now parse the following text and produce the JSON with exactly those keys, retaining everything:

{full_text_content}
                """

                response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {
                            "role": "system",
                            "content": "You are a helpful assistant that extracts OSCE station data from text and formats it as JSON."
                        },
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ],
                    temperature=0,
                    max_tokens=None
                )

                # Parse the JSON output from OpenAI
                json_response = response.choices[0].message.content.strip()

                # Clean any ```json fences
                json_response = json_response.replace("```json", "").replace("```", "").strip()

                parsed_data = json.loads(json_response)
                if isinstance(parsed_data, dict):
                    for key, station_data in parsed_data.items():
                        data_list.append(station_data)
                else:
                    # If somehow not a dict, just append directly
                    data_list.append(parsed_data)

                st.success(f"Successfully parsed **{file_name}**.")
                break

            except json.JSONDecodeError as json_error:
                if attempt < max_retries - 1:
                    st.warning(f"Error parsing JSON for {file_name}. Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    st.error(f"Error parsing JSON for {file_name} after {max_retries} attempts: {json_error}")
                    st.error(f"Raw response: {json_response}")
                    any_errors = True

            except Exception as e:
                st.error(f"Error processing {file_name}: {e}")
                any_errors = True
                break

    if data_list:
        st.write("### Parsed Data:")
        st.json(data_list)

        # Confirm before uploading
        if st.button("Upload Data to Supabase"):
            st.write("Uploading data to Supabase...")
            upload_errors = False
            for record in data_list:
                try:
                    # Upsert each station into staticOSCE table
                    response = supabase.table("staticOSCE").upsert(
                        record,
                        on_conflict="id"  # or any unique column
                    ).execute()

                    if response.data is not None or len(response.data) > 0:
                        st.success("Successfully upserted record into staticOSCE table.")
                    elif hasattr(response, 'error') and response.error:
                        st.error(f"Error uploading record: {response.error}")
                        upload_errors = True
                    else:
                        st.info("Record processed. No data returned (normal for upsert operations).")

                except Exception as e:
                    st.error(f"Exception during upload: {e}")
                    upload_errors = True

            if not upload_errors:
                st.success("All data processed successfully.")
            else:
                st.warning("Some data may have failed to upload. Please check the messages above.")
    else:
        if any_errors:
            st.warning("No data to upload due to errors in processing files.")
        else:
            st.warning("No data was parsed from the uploaded files.")
else:
    st.write("No files uploaded.")
