BUILDSPRINT

ROUND 2 • BUILD & DEMONSTRATE

CLI-Driven AI Chat Execution for Complex Tasks

Participant Challenge Brief



THIS IS A BUILD ROUND
You must develop a working prototype and submit a video explaining and demonstrating it. A concept note, architecture diagram, or slides alone will not satisfy the submission requirements.

1. Challenge Overview

AI chat applications commonly send a user's prompt to a language model and display the generated response. However, many practical requests require the system to do more than generate text: it may need to browse websites, collect information, process files, run scripts, transform data, and return verified results.

Your task is to build a working AI-powered chat application that can understand a user's request, invoke a CLI-based tool or agent through the backend, execute a real task, capture the execution output, and return the result in the chat interface.

2. Problem Statement

Design and implement a conversational AI system that translates natural-language instructions into controlled, multi-step tasks and executes those tasks by invoking a Command Line Interface (CLI) tool or agent from the backend. The system must capture the CLI execution status and output, process or validate the result, and present the final response to the user.

At minimum, demonstrate this approach through a website-crawling use case: a user provides a website URL and asks the system to crawl relevant pages, summarise the website, and/or return extracted information in structured JSON.

3. What You Need to Build

Build a usable prototype with the following main parts:





A chat interface where a user can enter a natural-language task and provide any required input, such as a URL.



An AI or intent-understanding layer that interprets the request and determines what action should be performed.



A backend orchestration layer that selects and invokes an approved CLI tool or agent.



A real execution workflow that performs the requested operation (for example, crawling and extracting website content).



An output-capture mechanism that collects the CLI result, standard output/error, process status, or structured result.



A response layer that displays the result clearly in the chat, including valid JSON when requested.



Basic handling for invalid inputs, execution failures, timeouts, or inaccessible websites.

4. Mandatory Demonstration Use Case

Your prototype must demonstrate at least one complete end-to-end task based on this example:

“Crawl https://example.com, extract the important information, give me a summary, and return the extracted data in JSON format.”

The system should:





Accept the URL and task through the chat interface.



Interpret the request and prepare the execution steps.



Invoke the CLI workflow from the backend (not merely describe a command in the AI response).



Actually access and process the permitted website content using the selected crawler/browser/parser.



Capture the execution result and determine whether the task succeeded or failed.



Return a readable summary and structured JSON to the chat interface.

You may use a different public website for the demonstration. Only crawl websites you are permitted to access, respect website terms and robots restrictions where applicable, and do not attempt to bypass authentication or access controls.

5. Functional Requirements







Requirement



Expected behaviour





Natural-language task input



The user should be able to describe the task in ordinary language.





Intent interpretation



The system should identify the task type, inputs, and expected output.





CLI invocation



The backend must programmatically start a CLI tool, process, or agent to perform the task.





Actual task execution



The CLI workflow must do real work; a mocked response or hard-coded sample result is not sufficient for the core demonstration.





Execution monitoring



The application should capture a meaningful execution status and handle completion or failure.





Output processing



The application should parse, clean, or validate the result before presenting it.





Chat response



The final response should be visible in the chat and should clearly distinguish actual results from errors or partial results.





JSON response



When JSON is requested, return syntactically valid structured JSON with clearly named fields.

6. CLI Execution: The Core of the Challenge

The central requirement is that your backend must invoke a CLI process or CLI-based agent to execute the task. Simply calling an LLM API and returning generated text does not meet the core objective.

Your implementation should explain and demonstrate:





Which CLI tool or agent is being invoked and why it was selected.



How the backend passes validated inputs and arguments to the CLI.



How the application captures stdout, stderr, exit code, or a structured output file/result.



How long-running tasks, timeouts, cancellation, and process failures are handled.



How the captured output is converted into the final chat response.

Illustrative pseudocode (you may implement this differently):

task = validate_user_request(prompt, url)
process = start_approved_cli(tool="site-crawler", args=task.args)
stdout, stderr, exit_code = collect_process_output(process)
result = validate_and_parse(stdout, exit_code)
return format_chat_response(result, stderr)

7. Freedom to Use AI Tools and Technology

You are free to use AI tools, coding assistants, agent frameworks, libraries, open-source projects, and any suitable technology stack to build your solution. You may use tools such as AI coding assistants, LLM APIs, browser automation frameworks, web-scraping libraries, CLI agents, and cloud or local development environments.

There is no mandatory programming language, framework, LLM provider, or CLI tool. Choose the tools that help you build the most effective working solution.

However, you must understand your implementation and be able to explain the architecture, the role of the CLI, how execution is triggered, how results are captured, and what parts were built with AI assistance. Mention the major AI tools and external services used in your README or video.

8. Security and Responsible Execution

Your prototype should demonstrate sensible safeguards. At minimum, consider:





Do not pass raw user or model-generated text directly into an unrestricted shell command.



Use approved commands/tools and validate inputs such as URLs and file paths.



Avoid unrestricted shell access; apply least-privilege permissions and sandboxing where feasible.



Set reasonable timeouts, crawl depth/page limits, and request limits.



Treat website content as untrusted input; it must not be allowed to change system instructions or trigger unrelated commands.



Do not expose API keys, environment variables, credentials, or other secrets in chat output or logs.



Show an honest error or partial-result status when execution fails; never claim a task succeeded without evidence.

9. Submission Deliverables

Each participant/team must submit all of the following:





1. Working application/prototype — A runnable implementation demonstrating the chat-to-CLI execution flow and at least one real end-to-end task.



2. Source code — A Git repository link or source-code package with the implementation. Include the required configuration/setup instructions. Do not commit API keys or secrets.



3. README/setup guide — Explain prerequisites, installation, environment variables, how to run the application, which CLI tool is used, and how to test the demo.



4. Live demo or access instructions — Provide a deployed URL if available. If the project runs locally, provide clear setup and run instructions so the judges can reproduce it.



5. One explanation and demonstration video — Submit one video that explains the solution and shows the working application. The video must include a real execution of the mandatory use case.

10. Explanation Video Requirements

Submit one video. A recommended duration is 4–7 minutes. Keep the explanation clear and show the actual application in use; a slide presentation without a product demonstration is not sufficient.





Briefly introduce the problem and your proposed solution.



Show the system architecture and explain the key components.



Explain how the natural-language prompt is converted into a CLI task.



Show the backend invoking the CLI and, where practical, show execution logs/status or captured output.



Run the website-crawling example live and show the actual summary and JSON result in the chat.



Briefly demonstrate one error/edge case or explain how your system handles it.



State the technology stack and disclose the main AI tools/services used.

The video should be a single recording or a single edited video file/link. Ensure the screen and audio are clear and the demo result is readable.

11. Evaluation Criteria

Suggested evaluation rubric (100 marks):







Evaluation area



What will be assessed



Marks





Working implementation



A functioning prototype; real task execution rather than a mock or static response



25





CLI integration



Correct backend invocation, argument handling, process monitoring, and output capture



20





End-to-end task quality



Website crawling/extraction, useful summary, valid JSON, and chat presentation



15





Architecture and engineering



Clear component design, code organisation, technology choices, and implementation quality



15





Security and reliability



Input validation, controlled execution, limits, error handling, and honest status reporting



10





Explanation video



Clear walkthrough, technical explanation, and convincing live/recorded demonstration



10





Originality and user experience



Thoughtful features, usability, and useful improvements beyond the minimum



5





TOTAL







100

12. What Will Not Be Considered a Complete Submission





A proposal, architecture diagram, or slide deck without a working prototype.



A chat UI that only sends prompts to an LLM and displays generated text, without invoking a CLI for real execution.



A hard-coded or fabricated crawl result presented as if it came from a live execution.



A video that explains the idea but does not show the application performing the task.



A repository that cannot be run because setup instructions or essential files are missing.

13. Submission

Submit all required deliverables using the official submission form:

https://form.workelate.com/previewform/6af8bd0090174e8d87484e41

Submission deadline: 16 November 2026, 11:59 PM IST.

14. Final Note

Build a solution that proves the concept in practice. The important part is not which AI tool or framework you choose; it is whether your application can take a user's request, invoke a controlled CLI workflow, perform a real task, capture what happened, and return an accurate result through chat. Be prepared to explain every major part of your implementation.

BUILD • EXECUTE • DEMONSTRATE