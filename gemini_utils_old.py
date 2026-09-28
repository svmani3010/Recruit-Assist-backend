import json
import re
import time
from google import genai
from google.genai import types
from datetime import datetime
from sp_manager import SystemPromptManager

class GeminiAPI:
    def __init__(self, api_key: str, prompt_dir: str = "prompts"):
        self.api_key = api_key
        self.client = genai.Client(api_key=self.api_key)
        self.model_pro = "gemini-3.1-pro-preview"
        self.model_flash = "gemini-2.5-flash"
        self.prompt_manager = SystemPromptManager(prompt_dir=prompt_dir)

    def resume_parser(self, resume_text: str) -> dict:
        print("[DEBUG] Resume parsing started")
        # sp_resume = self.prompt_manager.get_system_prompt('resume')
        manager = SystemPromptManager(prompt_dir='prompts')
        sp_resume = manager.get_system_prompt('resume')

        if not sp_resume:
            raise ValueError("System prompt 'resume' not found in prompts directory")

        model = self.model_flash
        
        contents = resume_text

        generate_content_config = types.GenerateContentConfig(
        temperature=0.8,

        thinking_config = types.ThinkingConfig(thinking_budget=-1,),

        response_mime_type="application/json",
        
        response_schema=genai.types.Schema(
            type = genai.types.Type.OBJECT,
            description = "Schema for a parsed resume from raw text. Extract information from the provided text to populate all fields. All fields are mandatory.",
            required = ["personal_information", "objective", "summary", "education", "skills", "work_experience", "projects", "certifications", "awards_and_honors", "publications", "languages", "referrals", "other_information"],
            properties = {
                "personal_information": genai.types.Schema(
                    type = genai.types.Type.OBJECT,
                    description = "All personal contact information for the candidate. All fields are mandatory.",
                    required = ["full_name", "email", "phone", "location", "linkedin", "portfolio_url"],
                    properties = {
                        "full_name": genai.types.Schema(
                            type = genai.types.Type.STRING,
                            description = "The full name of the candidate.",
                        ),
                        "email": genai.types.Schema(
                            type = genai.types.Type.STRING,
                            description = "The email address of the candidate.",
                        ),
                        "phone": genai.types.Schema(
                            type = genai.types.Type.STRING,
                            description = "The phone number of the candidate.",
                        ),
                        "location": genai.types.Schema(
                            type = genai.types.Type.STRING,
                            description = "The city and state of the candidate, e.g., 'San Francisco, CA'.",
                        ),
                        "linkedin": genai.types.Schema(
                            type = genai.types.Type.STRING,
                            description = "URL to the candidate's LinkedIn profile.",
                        ),
                        "portfolio_url": genai.types.Schema(
                            type = genai.types.Type.STRING,
                            description = "URL to the candidate's personal portfolio or GitHub.",
                        ),
                    },
                ),
                "objective": genai.types.Schema(
                    type = genai.types.Type.STRING,
                    description = "The career objective statement from the resume.",
                ),
                "summary": genai.types.Schema(
                    type = genai.types.Type.STRING,
                    description = "The professional summary section of the resume.",
                ),
                "education": genai.types.Schema(
                    type = genai.types.Type.ARRAY,
                    description = "A list of all educational qualifications. All fields are mandatory.",
                    items = genai.types.Schema(
                        type = genai.types.Type.OBJECT,
                        required = ["degree", "field_of_study", "institution", "start_date", "end_date", "grade", "gpa"],
                        properties = {
                            "degree": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The degree obtained, e.g., 'Bachelor of Science'.",
                            ),
                            "field_of_study": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The major or field of study, e.g., 'Computer Science'.",
                            ),
                            "institution": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The name of the university or institution.",
                            ),
                            "start_date": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The start date of education.",
                            ),
                            "end_date": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The end date or expected graduation date.",
                            ),
                            "grade": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The grade or classification, e.g., 'Summa Cum Laude'.",
                            ),
                            "gpa": genai.types.Schema(
                                type = genai.types.Type.NUMBER,
                                description = "The Grade Point Average.",
                            ),
                        },
                    ),
                ),
                "skills": genai.types.Schema(
                    type = genai.types.Type.OBJECT,
                    description = "All skills listed in the resume, categorized. All fields are mandatory.",
                    required = ["technical_skills", "soft_skills", "tools_and_technologies"],
                    properties = {
                        "technical_skills": genai.types.Schema(
                            type = genai.types.Type.ARRAY,
                            description = "List of technical skills, languages, and frameworks.",
                            items = genai.types.Schema(
                                type = genai.types.Type.STRING,
                            ),
                        ),
                        "soft_skills": genai.types.Schema(
                            type = genai.types.Type.ARRAY,
                            description = "List of soft skills.",
                            items = genai.types.Schema(
                                type = genai.types.Type.STRING,
                            ),
                        ),
                        "tools_and_technologies": genai.types.Schema(
                            type = genai.types.Type.ARRAY,
                            description = "List of specific tools and technologies.",
                            items = genai.types.Schema(
                                type = genai.types.Type.STRING,
                            ),
                        ),
                    },
                ),
                "work_experience": genai.types.Schema(
                    type = genai.types.Type.ARRAY,
                    description = "A list of all previous jobs and work experiences. All fields are mandatory.",
                    items = genai.types.Schema(
                        type = genai.types.Type.OBJECT,
                        required = ["job_title", "organization", "location", "start_date", "end_date", "description", "responsibilities", "achievements"],
                        properties = {
                            "job_title": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The job title or position held.",
                            ),
                            "organization": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The name of the company or organization.",
                            ),
                            "location": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The location of the job.",
                            ),
                            "start_date": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The start date of the employment.",
                            ),
                            "end_date": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The end date of the employment (or 'Present').",
                            ),
                            "description": genai.types.Schema(
                                type = genai.types.Type.ARRAY,
                                description = "A list of sentences describing the overall role.",
                                items = genai.types.Schema(
                                    type = genai.types.Type.STRING,
                                ),
                            ),
                            "responsibilities": genai.types.Schema(
                                type = genai.types.Type.ARRAY,
                                description = "A bulleted list of key responsibilities.",
                                items = genai.types.Schema(
                                    type = genai.types.Type.STRING,
                                ),
                            ),
                            "achievements": genai.types.Schema(
                                type = genai.types.Type.ARRAY,
                                description = "A bulleted list of notable achievements.",
                                items = genai.types.Schema(
                                    type = genai.types.Type.STRING,
                                ),
                            ),
                        },
                    ),
                ),
                "projects": genai.types.Schema(
                    type = genai.types.Type.ARRAY,
                    description = "A list of projects the candidate has worked on. All fields are mandatory.",
                    items = genai.types.Schema(
                        type = genai.types.Type.OBJECT,
                        required = ["project_name", "description", "technologies_used", "role", "link"],
                        properties = {
                            "project_name": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The name of the project.",
                            ),
                            "description": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "A brief description of the project.",
                            ),
                            "technologies_used": genai.types.Schema(
                                type = genai.types.Type.ARRAY,
                                description = "List of technologies used in the project.",
                                items = genai.types.Schema(
                                    type = genai.types.Type.STRING,
                                ),
                            ),
                            "role": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The candidate's role in the project.",
                            ),
                            "link": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "A URL to the project repository or live version.",
                            ),
                        },
                    ),
                ),
                "certifications": genai.types.Schema(
                    type = genai.types.Type.ARRAY,
                    description = "A list of professional certifications. All fields are mandatory.",
                    items = genai.types.Schema(
                        type = genai.types.Type.OBJECT,
                        required = ["name", "issuer", "date_issued", "expiration_date"],
                        properties = {
                            "name": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The name of the certification.",
                            ),
                            "issuer": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The organization that issued the certification.",
                            ),
                            "date_issued": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The date the certification was issued.",
                            ),
                            "expiration_date": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The expiration date, if applicable.",
                            ),
                        },
                    ),
                ),
                "awards_and_honors": genai.types.Schema(
                    type = genai.types.Type.ARRAY,
                    description = "List of any awards or honors.",
                    items = genai.types.Schema(
                        type = genai.types.Type.STRING,
                    ),
                ),
                "publications": genai.types.Schema(
                    type = genai.types.Type.ARRAY,
                    description = "List of any publications.",
                    items = genai.types.Schema(
                        type = genai.types.Type.STRING,
                    ),
                ),
                "languages": genai.types.Schema(
                    type = genai.types.Type.ARRAY,
                    description = "List of languages spoken by the candidate.",
                    items = genai.types.Schema(
                        type = genai.types.Type.STRING,
                    ),
                ),
                "referrals": genai.types.Schema(
                    type = genai.types.Type.ARRAY,
                    description = "Any referrals mentioned in the resume.",
                    items = genai.types.Schema(
                        type = genai.types.Type.STRING,
                    ),
                ),
                "other_information": genai.types.Schema(
                    type = genai.types.Type.ARRAY,
                    description = "A list of key-value pairs for any other miscellaneous information. All fields are mandatory.",
                    items = genai.types.Schema(
                        type = genai.types.Type.OBJECT,
                        required = ["key", "value"],
                        properties = {
                            "key": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The label or topic of the miscellaneous information (e.g., 'Hobbies', 'Availability').",
                            ),
                            "value": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The details of the miscellaneous information (e.g., 'Hiking, Reading', 'Available Immediately').",
                            ),
                        },
                    ),
                ),
            }
        ),
        # generate_content_config = types.GenerateContentConfig(
        #     temperature=0.8,
        #     thinking_config=types.ThinkingConfig(thinking_budget=-1),
        #     response_mime_type="application/json",
        #     response_schema=response_schema,
        # )
        system_instruction=[types.Part.from_text(text=sp_resume),],
    )
        # max_retries = 5
        # for attempt in range(max_retries):
            # try:
            #     raw_response = self.client.models.generate_content(
            #         model=model,
            #         contents=contents,
            #         config=generate_content_config,
            #     )
            #     result = json.loads(raw_response.text)
            #     return result
            # except genai.errors.ServerError as e:
            #     if e.code == 503 and attempt < max_retries - 1:
            #         wait_time = 2 ** attempt
            #         print(f"[Gemini] 503 ServerError, retrying in {wait_time} seconds...")
            #         time.sleep(wait_time)
            #     else:
            #         raise RuntimeError(f"Gemini API call failed: {e}")
            # except Exception as e:
            #     raise RuntimeError(f"Gemini API call failed: {e}")
        
        # Gemini API Call
        try:
            raw_response = self.client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=generate_content_config,
            )
        
            parsed_response = raw_response.text
            json_result = json.loads(parsed_response)
            return json_result
        
        except Exception as e:
            raise RuntimeError(f"Gemini API call failed: {e}")



    def jd_parser(self, jd_text):

        # Load System prompt
        # sp_jd = self.prompt_manager.get_system_prompt('jd')

        # Load System prompt
        manager = SystemPromptManager(prompt_dir='prompts')
        sp_jd = manager.get_system_prompt('jd')
        
        # Gemini API config
        model = self.model_flash

        contents = jd_text

        generate_content_config = types.GenerateContentConfig(
        temperature=0.8,

        thinking_config = types.ThinkingConfig(thinking_budget=-1,),

        response_mime_type="application/json",

        response_schema=genai.types.Schema(
            type = genai.types.Type.OBJECT,
            description = "Schema for parsing a job description from raw text. Extract information to populate all fields. All fields are mandatory.",
            required = ["job_information", "summary", "responsibilities", "requirements", "preferred_qualifications", "experience_required", "benefits", "salary_range", "employment_benefits", "application_deadline", "other_information"],
            properties = {
                "job_information": genai.types.Schema(
                    type = genai.types.Type.OBJECT,
                    description = "Core details about the job role and its context.",
                    required = ["job_title", "department", "employment_type", "location", "remote_policy"],
                    properties = {
                        "job_title": genai.types.Schema(
                            type = genai.types.Type.STRING,
                            description = "The official title of the job.",
                        ),
                        "department": genai.types.Schema(
                            type = genai.types.Type.STRING,
                            description = "The department or team the role belongs to.",
                        ),
                        "employment_type": genai.types.Schema(
                            type = genai.types.Type.STRING,
                            description = "The type of employment (e.g., 'Full-time', 'Contract', 'Internship').",
                        ),
                        "location": genai.types.Schema(
                            type = genai.types.Type.STRING,
                            description = "The physical location of the job (e.g., 'San Francisco, CA', 'London, UK').",
                        ),
                        "remote_policy": genai.types.Schema(
                            type = genai.types.Type.STRING,
                            description = "The policy on remote work (e.g., 'Fully Remote', 'Hybrid', 'On-site').",
                        ),
                    },
                ),
                "summary": genai.types.Schema(
                    type = genai.types.Type.STRING,
                    description = "A brief summary or overview of the job role.",
                ),
                "responsibilities": genai.types.Schema(
                    type = genai.types.Type.ARRAY,
                    description = "A list of the key responsibilities and day-to-day tasks for this role.",
                    items = genai.types.Schema(
                        type = genai.types.Type.STRING,
                    ),
                ),
                "requirements": genai.types.Schema(
                    type = genai.types.Type.OBJECT,
                    description = "The mandatory qualifications and skills required for the job.",
                    required = ["education", "technical_skills", "soft_skills", "tools_and_technologies", "certifications_required"],
                    properties = {
                        "education": genai.types.Schema(
                            type = genai.types.Type.ARRAY,
                            description = "Required educational background.",
                            items = genai.types.Schema(
                                type = genai.types.Type.OBJECT,
                                required = ["degree", "field_of_study"],
                                properties = {
                                    "degree": genai.types.Schema(
                                        type = genai.types.Type.STRING,
                                        description = "The required degree (e.g., 'Bachelor of Science').",
                                    ),
                                    "field_of_study": genai.types.Schema(
                                        type = genai.types.Type.STRING,
                                        description = "The required field of study (e.g., 'Computer Science').",
                                    ),
                                },
                            ),
                        ),
                        "technical_skills": genai.types.Schema(
                            type = genai.types.Type.ARRAY,
                            description = "List of required technical skills.",
                            items = genai.types.Schema(
                                type = genai.types.Type.STRING,
                            ),
                        ),
                        "soft_skills": genai.types.Schema(
                            type = genai.types.Type.ARRAY,
                            description = "List of required soft skills.",
                            items = genai.types.Schema(
                                type = genai.types.Type.STRING,
                            ),
                        ),
                        "tools_and_technologies": genai.types.Schema(
                            type = genai.types.Type.ARRAY,
                            description = "List of required tools and technologies.",
                            items = genai.types.Schema(
                                type = genai.types.Type.STRING,
                            ),
                        ),
                        "certifications_required": genai.types.Schema(
                            type = genai.types.Type.ARRAY,
                            description = "List of mandatory certifications.",
                            items = genai.types.Schema(
                                type = genai.types.Type.STRING,
                            ),
                        ),
                    },
                ),
                "preferred_qualifications": genai.types.Schema(
                    type = genai.types.Type.OBJECT,
                    description = "The 'nice-to-have' qualifications and skills that are preferred but not mandatory.",
                    required = ["education", "technical_skills", "soft_skills", "tools_and_technologies", "certifications_preferred"],
                    properties = {
                        "education": genai.types.Schema(
                            type = genai.types.Type.ARRAY,
                            description = "Preferred educational background.",
                            items = genai.types.Schema(
                                type = genai.types.Type.OBJECT,
                                required = ["degree", "field_of_study"],
                                properties = {
                                    "degree": genai.types.Schema(
                                        type = genai.types.Type.STRING,
                                        description = "The preferred degree.",
                                    ),
                                    "field_of_study": genai.types.Schema(
                                        type = genai.types.Type.STRING,
                                        description = "The preferred field of study.",
                                    ),
                                },
                            ),
                        ),
                        "technical_skills": genai.types.Schema(
                            type = genai.types.Type.ARRAY,
                            description = "List of preferred technical skills.",
                            items = genai.types.Schema(
                                type = genai.types.Type.STRING,
                            ),
                        ),
                        "soft_skills": genai.types.Schema(
                            type = genai.types.Type.ARRAY,
                            description = "List of preferred soft skills.",
                            items = genai.types.Schema(
                                type = genai.types.Type.STRING,
                            ),
                        ),
                        "tools_and_technologies": genai.types.Schema(
                            type = genai.types.Type.ARRAY,
                            description = "List of preferred tools and technologies.",
                            items = genai.types.Schema(
                                type = genai.types.Type.STRING,
                            ),
                        ),
                        "certifications_preferred": genai.types.Schema(
                            type = genai.types.Type.ARRAY,
                            description = "List of preferred certifications.",
                            items = genai.types.Schema(
                                type = genai.types.Type.STRING,
                            ),
                        ),
                    },
                ),
                "experience_required": genai.types.Schema(
                    type = genai.types.Type.OBJECT,
                    description = "Details about the required level and type of professional experience.",
                    required = ["total_years", "relevant_experience_years", "domains"],
                    properties = {
                        "total_years": genai.types.Schema(
                            type = genai.types.Type.STRING,
                            description = "The total years of professional experience required (e.g., '5+ years').",
                        ),
                        "relevant_experience_years": genai.types.Schema(
                            type = genai.types.Type.STRING,
                            description = "The years of experience required in a specific, relevant field (e.g., '3+ years in data engineering').",
                        ),
                        "domains": genai.types.Schema(
                            type = genai.types.Type.ARRAY,
                            description = "Specific industries or domains where experience is required.",
                            items = genai.types.Schema(
                                type = genai.types.Type.STRING,
                            ),
                        ),
                    },
                ),
                "benefits": genai.types.Schema(
                    type = genai.types.Type.ARRAY,
                    description = "A list of general company benefits.",
                    items = genai.types.Schema(
                        type = genai.types.Type.STRING,
                    ),
                ),
                "salary_range": genai.types.Schema(
                    type = genai.types.Type.OBJECT,
                    description = "The salary range for the position.",
                    required = ["min", "max", "currency"],
                    properties = {
                        "min": genai.types.Schema(
                            type = genai.types.Type.NUMBER,
                            description = "The minimum salary for the role.",
                        ),
                        "max": genai.types.Schema(
                            type = genai.types.Type.NUMBER,
                            description = "The maximum salary for the role.",
                        ),
                        "currency": genai.types.Schema(
                            type = genai.types.Type.STRING,
                            description = "The currency of the salary (e.g., 'USD', 'EUR').",
                        ),
                    },
                ),
                "employment_benefits": genai.types.Schema(
                    type = genai.types.Type.ARRAY,
                    description = "Specific benefits tied to employment, such as health insurance or retirement plans.",
                    items = genai.types.Schema(
                        type = genai.types.Type.STRING,
                    ),
                ),
                "application_deadline": genai.types.Schema(
                    type = genai.types.Type.STRING,
                    description = "The closing date for applications.",
                    format = "date-time",
                ),
                "other_information": genai.types.Schema(
                    type = genai.types.Type.ARRAY,
                    description = "A list of key-value pairs for any other miscellaneous information.",
                    items = genai.types.Schema(
                        type = genai.types.Type.OBJECT,
                        required = ["key", "value"],
                        properties = {
                            "key": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The label or topic of the information (e.g., 'Travel Requirements').",
                            ),
                            "value": genai.types.Schema(
                                type = genai.types.Type.STRING,
                                description = "The details of the information (e.g., '10% travel required').",
                            ),
                        },
                    ),
                ),
            },
        ),
        system_instruction=[types.Part.from_text(text=sp_jd),],
    )
        

        # Gemini API Call
        try:
            raw_response = self.client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=generate_content_config,
            )
        
            parsed_response = raw_response.text
            json_result = json.loads(parsed_response)
            result = json.dumps(json_result, indent=2)

            return result 
        
        except Exception as e:
            raise RuntimeError(f"Gemini API call failed: {e}")

        
    def initial_screening(self, lite_resume_data, lite_jd_data):

        # Load System prompt
        manager = SystemPromptManager(prompt_dir='prompts')
        sp_initial = manager.get_system_prompt('initial')
        
        current_datetime = datetime.now().strftime("%d-%m-%Y")

        # User Prompt Template
        template = """
Resume Data:
{lite_resume_data}
        
---

Job Description:
{lite_jd_data}

---

Current Date: {current_datetime}
        """

        prompt = template.format( lite_resume_data=json.dumps(lite_resume_data, indent=2) , lite_jd_data=json.dumps(lite_jd_data, indent=2), current_datetime=current_datetime )

        # Gemini API config
        model = self.model_pro

        contents = prompt

        generate_content_config = types.GenerateContentConfig(
        temperature=0,
        
        thinking_config = types.ThinkingConfig(thinking_budget=-1,),

        response_mime_type="application/json",

        response_schema=genai.types.Schema(
            type = genai.types.Type.OBJECT,
            description = "Represents the compatibility score between a candidate's resume and a job description.",
            required = ["compatibility", "l0_reasons"],
            properties = {
                "compatibility": genai.types.Schema(
                    type = genai.types.Type.STRING,
                    description = "The calculated compatibility score, expressed as a percentage string (e.g., '82%').",
                ),
                "l0_reasons": genai.types.Schema(
                    type = genai.types.Type.ARRAY,
                    description = "A list of key reasons explaining the screening result (pass or fail), highlighting the most important factors.",
                    items = genai.types.Schema(
                        type = genai.types.Type.STRING,
                    ),
                ),
            },
        ),
        
        system_instruction=[types.Part.from_text(text=sp_initial),],
    )

        # Gemini API Call
        try:
            raw_response = self.client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=generate_content_config,
            )
            
            parsed_response = raw_response.text
            json_result = json.loads(parsed_response)
            return json_result
         
        except Exception as e:
            raise RuntimeError(f"Gemini API call failed: {e}")

    def live_l1(self, resume_data, jd_data):
        try:
            manager = SystemPromptManager(prompt_dir='prompts')
            sp_live = manager.get_system_prompt('l1_v3')


            # sp_live = self.prompt_manager.get_system_prompt('l1_v3')
            current_datetime = datetime.now().strftime("%d-%m-%Y")
            print("##############################################")
            print(sp_live)
            if not sp_live:
                raise ValueError("System prompt 'live_l1' not found")

            template = """
Resume Data:
{resume_data}
            
---

Job Description:
{jd_data}

---

Current Date: {current_datetime}
            """

            initial_prompt = template.format( resume_data=json.dumps(resume_data, indent=2) , jd_data=json.dumps(jd_data, indent=2), current_datetime=current_datetime )

            model = self.model_pro
            tools = [types.Tool(googleSearch=types.GoogleSearch())]
            generate_content_config = types.GenerateContentConfig(
                temperature=1.1,
                thinking_config=types.ThinkingConfig(thinking_budget=-1),
                tools=tools,
                system_instruction=[types.Part.from_text(text=sp_live)],
            )

            # Use the long-lived Gemini client on the class instance.
            # A short-lived local client can be garbage-collected/closed while chat is still active.
            live_chat = self.client.chats.create(model=model, config=generate_content_config)

            print("------ Chat Session Started ------")
            print(f"chat name = {live_chat}")

            initial_response = live_chat.send_message(initial_prompt)
            print(f"AI initial response: {initial_response.text}")

            return live_chat, initial_response.text

        except Exception as e:
            raise RuntimeError(f"Gemini API call failed: {e}")
    # def question_generation(self, resume_data, jd_data):
    #     sp_question = self.prompt_manager.get_system_prompt('question')
    #     if not sp_question:
    #         raise ValueError("System prompt 'question' not found in prompts directory")

    #     template = """
    #     Resume Data:
    #     {resume_data}

    #     ---

    #     Job Description:
    #     {jd_data}
    #     """

    #     user_prompt = template.format(
    #         resume_data=json.dumps(resume_data, indent=2),
    #         jd_data=json.dumps(jd_data, indent=2)
    #     )

    #     contents = [types.Content(role="user", parts=[types.Part(text=user_prompt)])]

    #     config = types.GenerateContentConfig(
    #         temperature=1.1,
    #         thinking_config=types.ThinkingConfig(),
    #         system_instruction=[types.Part.from_text(text=sp_question)],
    #         tools=[],
    #     )

    #     def remove_markdown_fences(text: str) -> str:
    #         text = text.strip()
    #         text = re.sub(r'^\`{3}json\s*', '', text)  # Remove starting ```json
    #         text = re.sub(r'^\`{3}\s*', '', text)      # Remove starting ```
    #         text = re.sub(r'\s*\`{3}$', '', text)      # Remove ending ```
    #         return text.strip()

    #     try:
    #         raw_response = self.client.models.generate_content(
    #             model="gemini-2.5-flash", #pro
    #             contents=contents,
    #             config=config,
    #         )

    #         response_text = raw_response.text
    #         print("Gemini raw response:", response_text[:1500])

    #         cleaned_text = remove_markdown_fences(response_text)
    #         if not cleaned_text:
    #             raise ValueError("Empty response after cleaning markdown fences")

    #         result = json.loads(cleaned_text)
    #         return result

    #     except Exception as e:
    #         print(f"Error during Gemini API call or JSON parsing: {e}")
    #         raise e
        
    def complete_screening(self, complete_resume, complete_jd, section_weightages):
        manager = SystemPromptManager(prompt_dir='prompts')
        sp_complete = manager.get_system_prompt('complete')

        current_datetime = datetime.now().strftime("%d-%m-%Y")

        resume = json.loads(complete_resume)
        jd = json.loads(complete_jd)

        print("[DEBUG] complete_screening: resume:", json.dumps(resume)[:500])
        print("[DEBUG] complete_screening: jd:", json.dumps(jd)[:500])
        print("[DEBUG] complete_screening: section_weightages:", section_weightages)

        template = """
        Resume Data (JSON):
        {complete_resume}
        
        ---

        Job Description (JSON):
        {complete_jd}

        ---

        Section Weightages:
        {section_weightages}

        
        ---

        Current Date: {current_datetime}
        """

        prompt = template.format(
            complete_resume=json.dumps(resume, indent=2),
            complete_jd=json.dumps(jd, indent=2),
            section_weightages=section_weightages,
            current_datetime=current_datetime
        )
        model = self.model_pro
        contents = prompt

        generate_content_config = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=-1),
            response_mime_type="application/json",
            response_schema=genai.types.Schema(
                type=genai.types.Type.OBJECT,
                required=["llm_score", "llm_compatibility", "score_breakdown", "reasons"],
                properties={
                    "llm_score": genai.types.Schema(type=genai.types.Type.NUMBER),
                    "llm_compatibility": genai.types.Schema(type=genai.types.Type.STRING),
                    "score_breakdown": genai.types.Schema(
                        type=genai.types.Type.ARRAY,
                        items=genai.types.Schema(
                            type=genai.types.Type.OBJECT,
                            required=["category", "score"],
                            properties={
                                "category": genai.types.Schema(type=genai.types.Type.STRING),
                                "score": genai.types.Schema(type=genai.types.Type.STRING),
                            }
                        )
                    ),
                    "reasons": genai.types.Schema(
                        type=genai.types.Type.ARRAY,
                        items=genai.types.Schema(type=genai.types.Type.STRING),
                    ),
                }
            ),
            system_instruction=[types.Part.from_text(text=sp_complete)],
        )

        try:
            raw_response = self.client.models.generate_content(
                model=model,
                contents=contents,
                config=generate_content_config,
            )
            parsed_response = raw_response.text
            json_result = json.loads(parsed_response)
            #  result = json.dumps(json_result, indent=2)
            
            return json_result
        except Exception as e:
            raise RuntimeError(f"Gemini API call failed: {e}")
    
    def job_description_generator(self, job_title: str):
        try:
            print(f"[DEBUG] Loading prompt for job description generation")
            sp_jd = self.prompt_manager.get_system_prompt('job_description_prompt')
            # Escape all, then undo specifically for your real variable placeholder:
            # sp_jd = sp_jd.replace("{", "{{").replace("}", "}}").replace("{{job_name}}", "{job_name}")
            contents = job_title
            if not sp_jd:
                raise ValueError("System prompt 'job_description_prompt' not found")

            print(f"[DEBUG] Formatting prompt with job_title: {job_title}")

            # IMPORTANT: Ensure prompt braces are escaped except for {job_name}
            # If not handled externally, consider:
            # sp_jd = sp_jd.replace("{", "{{").replace("}", "}}").replace("{{job_name}}", "{job_name}")

            contents = sp_jd.format(job_name=job_title)

            print(f"[DEBUG] FULL PROMPT SENT TO GEMINI:\n{contents}")  # log first 500 chars

            response_schema = genai.types.Schema(
                type=genai.types.Type.OBJECT,
                required=[
                    "Job Description",
                    "Educational Qualifications",
                    "Experience Required",
                    "Desired Skills",
                    "Good to Have",
                ],
                properties={
                    "Job Description": genai.types.Schema(type=genai.types.Type.STRING),
                    "Educational Qualifications": genai.types.Schema(type=genai.types.Type.STRING),
                    "Experience Required": genai.types.Schema(type=genai.types.Type.STRING),
                    "Desired Skills": genai.types.Schema(type=genai.types.Type.STRING),
                    "Good to Have": genai.types.Schema(type=genai.types.Type.STRING),
                }
            )

            generate_content_config = types.GenerateContentConfig(
                temperature=0.0,
                thinking_config=types.ThinkingConfig(thinking_budget=-1),
                response_mime_type="application/json",
                response_schema=response_schema,
                system_instruction=[types.Part.from_text(text=contents)],
            )

            max_retries = 5
            for attempt in range(max_retries):
                try:
                    print(f"[DEBUG] Calling Gemini API, attempt {attempt+1}")
                    raw_response = self.client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=contents,
                        config=generate_content_config,
                    )
                    print(f"[DEBUG] Gemini API raw response: {raw_response.text[:500]}")  # log first 500 chars
                    result = json.loads(raw_response.text)
                    return result
                except genai.errors.ServerError as e:
                    print(f"[WARNING] Gemini API ServerError: {e}")
                    if e.code == 503 and attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        print(f"[DEBUG] Retrying after {wait_time} seconds...")
                        time.sleep(wait_time)
                    else:
                        print(f"[ERROR] Gemini API call failed after retries: {e}")
                        raise RuntimeError(f"Gemini API call failed: {e}")
                except Exception as e:
                    print(f"[ERROR] Exception in Gemini API call: {e}")
                    raise RuntimeError(f"Gemini API call failed: {e}")

        except Exception as e:
            print(f"[ERROR] Exception in job_description_generator: {e}")
            raise e
        
    
    def generate_l2_questions(self,resume_json, jd_json, resume_score, resume_reason, l1_report):
        manager = SystemPromptManager(prompt_dir='prompts')  # your prompt directory
        sp_l2 = manager.get_system_prompt('L2') 
        prompt = sp_l2.format(
            resume_json=json.dumps(resume_json, ensure_ascii=False),
            jd_json=json.dumps(jd_json, ensure_ascii=False),
            resume_score=resume_score,
            resume_reason=json.dumps(resume_reason, ensure_ascii=False),
            l1_report=json.dumps(l1_report, ensure_ascii=False)
        )

        response_schema = types.Schema(
            type=types.Type.OBJECT,
            required=["question"],
            properties={
                "question": types.Schema(
                    type=types.Type.ARRAY,
                    items=types.Schema(type=types.Type.STRING)
                )
            }
        )

        generate_content_config = types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=response_schema,
            system_instruction=[types.Part.from_text(text=prompt)],
        )

        model = "gemini-2.5-flash"
        max_retries = 3

        for attempt in range(max_retries):
            try:
                raw_response = self.client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=generate_content_config,
                )
                if not raw_response.text.strip():
                    raise RuntimeError("Empty response")

                result = json.loads(raw_response.text)
                if isinstance(result.get("question"), list) and len(result["question"]) > 0:
                    return result
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise RuntimeError(f"Failed after retries: {e}")

    def generate_interview_email(self, data: dict) -> str:
        prompt = f"""
        Generate a professional interview invitation email based on the following details:
        Company: {data.get('Company', 'Fanisko')}
        Candidate Name: {data.get('candidate_name')}
        Position: {data.get('position')}
        Interview Level: {data.get('interview_level', 'Technical Round')}
        Date: {data.get('date')}
        Time: {data.get('time')}
        Duration: {data.get('duration_minutes')} minutes
        Timezone: {data.get('timezone')}
        Interviewers: {', '.join([i.get('name') for i in data.get('interviewers', [])])}
        Meeting Link: {data.get('meeting_link', 'https://meet.google.com/new')}
        
        CRITICAL INSTRUCTIONS:
        1. DO NOT use placeholders like "[Position]", "[Date]", or "[Time]". 
        2. Use the ACTUAL values provided above: Use "{data.get('position')}" for the position.
        3. Format the interview details as a list.
        4. Bold the labels AND the values for position, date, and time. Example: **Position:** **{data.get('position')}**
        5. The email should be professional, welcoming, and encouraging.
        6. Return ONLY the email body text.
        """
        
        generate_content_config = types.GenerateContentConfig(
            temperature=0.7,
            thinking_config=types.ThinkingConfig(thinking_budget=-1),
        )
        
        response = self.client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=generate_content_config,
        )
        return response.text