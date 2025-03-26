import azure.functions as func
import azure.durable_functions as df
import logging
import requests
import os
import json
import time
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from azure.storage.queue import QueueClient, BinaryBase64EncodePolicy, BinaryBase64DecodePolicy
from datetime import datetime, timedelta

# Initialize the Durable Functions app with anonymous HTTP authentication level
app = df.DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Name of the queues to get and send the function call messages
input_queue_name = "input"
output_queue_name = "output"

def initialize_client():
    """
    Initialize the agent client and the tools Azure Functions that the agent can use.
    """
    # Create a project client using the connection string from local.settings.json
    project_client = AIProjectClient.from_connection_string(
        credential=DefaultAzureCredential(),
        conn_str=os.environ["PROJECT_CONNECTION_STRING"]
    )

    # Get the connection string from local.settings.json
    storage_connection_string = os.environ.get("STORAGE_CONNECTION__queueServiceUri")

    # Create an agent with the Azure Function tool to get the weather
    agent = project_client.agents.create_agent(
        model="gpt-4o-mini",
        name="azure-function-agent-summarize-github-issues",
        instructions="You are a helpful support agent. Answer the user's questions to the best of your ability.",
        headers={"x-ms-enable-preview": "true"},
        tools=[
            {
                "type": "azure_function",
                "azure_function": {
                    "function": {
                        "name": "GitHubIssuesSummaries",
                        "description": "Provide a summary of the GitHub issues for the organization within a specified time period.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "organization": {"type": "string", "description": "The organization to find GitHub issues for."},
                                "time": {"type": "string", "description": "The specific time period for which the user is querying GitHub issues."}
                            },
                            "required": ["time"]
                        }
                    },
                    "input_binding": {
                        "type": "storage_queue",
                        "storage_queue": {
                            "queue_service_uri": storage_connection_string,
                            "queue_name": input_queue_name
                        }
                    },
                    "output_binding": {
                        "type": "storage_queue",
                        "storage_queue": {
                            "queue_service_uri": storage_connection_string,
                            "queue_name": output_queue_name
                        }
                    }
                }
            }
        ],
    )
    logging.info(f"Created agent, agent ID: {agent.id}")

    # Create a thread for communication with the agent
    thread = project_client.agents.create_thread()
    logging.info(f"Created thread, thread ID: {thread.id}")

    return project_client, thread, agent

@app.route(route="prompt", auth_level=func.AuthLevel.FUNCTION)
def prompt(req: func.HttpRequest) -> func.HttpResponse:
    """
    HTTP trigger function to handle prompts and interact with the agent.
    """
    logging.info('Python HTTP trigger function processed a request.')

    # Handle OPTIONS request for CORS preflight
    if req.method == "OPTIONS":
        return func.HttpResponse(
            status_code=204,
            headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type'
            }
        )

    # Get the prompt from the request body
    req_body = req.get_json()
    prompt = req_body.get('Prompt')

    # Initialize the agent client
    project_client, thread, agent = initialize_client()

    # Send the prompt to the agent
    message = project_client.agents.create_message(
        thread_id=thread.id,
        role="user",
        content=prompt,
    )
    logging.info(f"Created message, message ID: {message.id}")

    # Run the agent and monitor its status
    run = project_client.agents.create_run(thread_id=thread.id, assistant_id=agent.id)
    
    while run.status in ["queued", "in_progress", "requires_action"]:
        time.sleep(1)
        run = project_client.agents.get_run(thread_id=thread.id, run_id=run.id)

        if run.status not in ["queued", "in_progress", "requires_action"]:
            break

    logging.info(f"Run finished with status: {run.status}")

    if run.status == "failed":
        logging.error(f"Run failed: {run.last_error}")

    # Get messages from the assistant thread and retrieve the last message from the assistant
    messages = project_client.agents.get_messages(thread_id=thread.id)
    logging.info(f"Messages: {messages}")

    last_msg = messages.get_last_text_message_by_sender("assistant")
    
    if last_msg:
        logging.info(f"Last Message: {last_msg.text.value}")

    # Delete the agent once done
    project_client.agents.delete_agent(agent.id)
    
    return func.HttpResponse(
        json.dumps({"message": last_msg.text.value}),
        mimetype="application/json",
        headers={
            'Access-Control-Allow-Origin': '*'
        }
    )

@app.function_name(name="GitHubIssuesSummaries")
@app.durable_client_input(client_name="client")
@app.queue_trigger(arg_name="msg", queue_name="input", connection="STORAGE_CONNECTION")  
async def process_queue_message(msg: func.QueueMessage, client) -> None:
    """
    Function to start orchestration when a message is received in the queue.
    """
    logging.info('Python queue trigger function processed a queue item')
    
    messagepayload = json.loads(msg.get_body().decode('utf-8'))

    instance_id = await client.start_new("SummarizeGitHubIssues", None, messagepayload)

    logging.info('Started orchestration with ID = {instance_id}')
    
@app.function_name(name="SummarizeGitHubIssues")
@app.orchestration_trigger(context_name="context")
def summarize_github_issues(context: df.DurableOrchestrationContext):
    """
    Orchestrator function to summarize GitHub issues.
    """
    first_retry_interval_in_milliseconds = 5000
    max_number_of_attempts = 3
    retry_options = df.RetryOptions(first_retry_interval_in_milliseconds, max_number_of_attempts)
    
    messagepayload = context.get_input()
    correlation_id = messagepayload['CorrelationId']
    organization = messagepayload.get('organization')
    repo = messagepayload.get('repo')
    
    # Initialize the time dictionary with actual values
    time = {
        "current_date_time": datetime.utcnow().isoformat() + 'Z',
        "prompt_time": messagepayload['time']
    }

    # Query repositories for the organization using an activity function
    if organization:
        repos = yield context.call_activity_with_retry("QueryRepos", retry_options, organization)
        repo_names = repos["repositories"]
    else:
        prompt = f"Return *only* the GitHub organization name for the repository: {repo}"
        organization = yield context.call_activity_with_retry("AskAOAI", retry_options, prompt)
        repo_names = [repo]

    # Correctly format the prompt string using f-string
    prompt = f"Assume the current time is {time['current_date_time']}. Convert the following time to ISO format and return *only* the converted time in the format 'YYYY-MM-DDTHH:MM:SSZ': {time['prompt_time']}"

    # Convert time to ISO format using an activity function
    converted_time = yield context.call_activity_with_retry("AskAOAI", retry_options, prompt)
    
    # Fan-out: Create a list of tasks to get issues for each repository
    tasks = []
    
    for repo_name in repo_names:
        task = context.call_activity_with_retry("QueryNewIssues", retry_options, {'repoName': repo_name, 'orgName': organization, 'time': converted_time})
        tasks.append(task)

    # Fan-in: Wait for all tasks to complete and aggregate the results
    all_issues = yield context.task_all(tasks)

    # Ensure all_issues is initialized before filtering
    if all_issues is None:
        all_issues = []

    # Filter out empty arrays of objects
    allIssues = filter_empty_issues(all_issues)

    # Call the activity function to generate a summary using Azure OpenAI
    summary = yield context.call_activity_with_retry("SummarizeIssues", retry_options, allIssues)

    logging.info(f"summary: {summary}")

    # Send message to queue. Sends a mock message for the weather
    result_message = {
        'Value': summary['content'],
        'CorrelationId': correlation_id
    }

    # Queue to send message to
    queue_client = QueueClient(
        os.environ["STORAGE_CONNECTION__queueServiceUri"],
        queue_name="output",
        credential=DefaultAzureCredential(),
        message_encode_policy=BinaryBase64EncodePolicy(),
        message_decode_policy=BinaryBase64DecodePolicy()
    )

    queue_client.send_message(json.dumps(result_message).encode('utf-8'))

@app.function_name(name="QueryRepos")
@app.activity_trigger(input_name='organization')
def get_repos(organization):
    """
    Activity function to get repositories.
    """
    access_token = os.environ.get("GITHUB_ACCESS_TOKEN")

    url = f"https://api.github.com/orgs/{organization}/repos"
    headers = {
        "Authorization": f"token {access_token}"
    }

    repo_names = []
    page = 1

    while True:
        response = requests.get(url, headers=headers, params={'page': page, 'per_page': 100})
        
        if response.status_code == 200:
            repos = response.json()
            if not repos:
                break
            for repo in repos:
                repo_names.append(repo['name'])
            page += 1
        else:
            print(f"Failed to retrieve repositories: {response.status_code}")
            break

    result = {
        "repositories": repo_names
    }

    return result

@app.function_name(name="AskAOAI")
@app.activity_trigger(input_name='prompt')
@app.generic_input_binding(arg_name="response", type="textCompletion", data_type=func.DataType.STRING, prompt = "{prompt}", model = "%CHAT_MODEL_DEPLOYMENT_NAME%")
def ask_llm(prompt, response: str):
    """
    Activity function to convert the time.
    """
    logging.info(f"in ConvertTime activity")
    response_json = json.loads(response)
    logging.info(response_json['content'])
    return response_json['content'] 
   
@app.function_name(name="QueryNewIssues")
@app.activity_trigger(input_name='queryDetails')
def get_issues(queryDetails):
    """
    Activity function to get issues for a repository.
    """
    organization = queryDetails['orgName']
    repoName = queryDetails['repoName']
    since = queryDetails['time']

    access_token = os.environ.get("GITHUB_ACCESS_TOKEN")

    url = f"https://api.github.com/repos/{organization}/{repoName}/issues"

    headers = {
        "Authorization": f"token {access_token}"
    }
    
    params = {
        "since": since,
    }

    response = requests.get(url, headers=headers, params=params)
    issues = []

    if response.status_code == 200:
        issues_data = response.json()
        if issues_data:
            repo_issues = []
            for issue in issues_data:
                filtered_issue = {
                    "state": issue.get("state"),
                    "user": issue.get("user", {}).get("login"),
                    "title": issue.get("title"),
                    "body": issue.get("body")
                }
                repo_issues.append(filtered_issue)
            issues.append({repoName: repo_issues})
    else:
        print(f"Failed to retrieve issues for {repoName}: {response.status_code}")

    return issues

@app.function_name(name="SummarizeIssues")
@app.activity_trigger(input_name='allIssues')
@app.generic_input_binding(arg_name="response", type="textCompletion", max_tokens="1,000", data_type=func.DataType.STRING, prompt="Generate a summary of the following GitHub issues and determine: {allIssues}", model = "%CHAT_MODEL_DEPLOYMENT_NAME%")
def summarize_issues(allIssues, response: str):
    """
    Activity function to generate a summary using Azure OpenAI.
    """
    logging.info(f"in summarize_text activity")
    response_json = json.loads(response)
    logging.info(response_json['content'])
    return response_json 

def filter_empty_issues(allIssues):
    """
    Function to filter out empty arrays of objects.
    """
    return [issue for issue in allIssues if issue]