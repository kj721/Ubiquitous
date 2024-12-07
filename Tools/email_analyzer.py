import win32com.client
import pandas as pd
from collections import Counter
import re
from datetime import datetime

# Define stopwords - common words to exclude
STOPWORDS = {
    'and', 'the', 'to', 'of', 'in', 'a', 'is', 'that', 'for', 'on', 'with',
    'this', 'are', 'was', 'as', 'at', 'be', 'by', 'have', 'has', 'had',
    'it', 'its', "it's", 'from', 'or', 'an', 'they', 'these', 'those',
    'but', 'not', 'what', 'all', 'were', 'when', 'where', 'who', 'which',
    'why', 'how', 'can', 'could', 'would', 'should', 'will', 'just',
    'than', 'then', 'now', 'into', 'out', 'only', 'your', 'you',
    'may', 'more', 'such', 'about', 'many', 'some', 'one', 'through',
    'also', 'been', 'being', 'does', 'doing', 'any', 'use', 'using'
}

def list_folders(parent_folder, indent=""):
    """
    Helper function to list all folders and their contents
    """
    try:
        for folder in parent_folder.Folders:
            print(f"{indent}└── {folder.Name}")
            list_folders(folder, indent + "    ")
    except Exception as e:
        pass

def extract_outlook_emails():
    """
    Extract emails from the specified Outlook folder path
    """
    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        namespace = outlook.GetNamespace("MAPI")
        
        # Get the inbox (6 is the Outlook folder index for inbox)
        inbox = namespace.GetDefaultFolder(6)
        
        # Print folder structure to help with debugging
        print("Folder structure:")
        print(f"Inbox")
        list_folders(inbox)
        
        # Navigate to the target folder
        try:
            year_folder = inbox.Folders["2024"]
            target_folder = year_folder.Folders["3. To Read"]
        except Exception as e:
            print(f"Error navigating to folder: {str(e)}")
            print("Please check the printed folder structure above and verify the folder names.")
            return []
        
        emails = []
        for item in target_folder.Items:
            try:
                emails.append({
                    'subject': item.Subject,
                    'body': item.Body,
                    'received': item.ReceivedTime,
                    'sender': item.SenderName
                })
            except Exception as e:
                print(f"Error processing email: {str(e)}")
                continue
        
        return emails
        
    except Exception as e:
        print(f"Error setting up Outlook connection: {str(e)}")
        return []

def analyze_keywords(emails, min_word_length=4, top_n=20, custom_stopwords=None):
    """
    Analyze keyword frequency across all emails, excluding stopwords
    """
    if not emails:
        print("No emails to analyze!")
        return []
        
    # Combine all email bodies
    all_text = ' '.join([email['body'] for email in emails])
    
    # Clean text
    words = re.findall(r'\b\w+\b', all_text.lower())
    
    # Combine default and custom stopwords
    all_stopwords = STOPWORDS
    if custom_stopwords:
        all_stopwords = all_stopwords.union(custom_stopwords)
    
    # Filter out short words, stopwords, and common email artifacts
    filtered_words = [
        word for word in words 
        if len(word) >= min_word_length
        and word not in all_stopwords
        and not word.startswith(('http', 'www'))
    ]
    
    # Count frequencies
    word_freq = Counter(filtered_words)
    
    return word_freq.most_common(top_n)

def save_results(emails, keywords, output_file='email_analysis.txt'):
    """
    Save emails and keyword analysis to a file
    """
    if not emails:
        print("No results to save!")
        return
        
    with open(output_file, 'w', encoding='utf-8') as f:
        # Write summary
        f.write(f"Email Analysis Report\n")
        f.write(f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Total Emails Analyzed: {len(emails)}\n\n")
        
        # Write keyword frequency
        f.write("Top Keywords (frequency):\n")
        for word, count in keywords:
            f.write(f"{word}: {count}\n")
        f.write("\n" + "="*50 + "\n\n")
        
        # Write full email contents
        f.write("Full Email Contents:\n\n")
        for i, email in enumerate(emails, 1):
            f.write(f"Email {i}:\n")
            f.write(f"From: {email['sender']}\n")
            f.write(f"Subject: {email['subject']}\n")
            f.write(f"Received: {email['received']}\n")
            f.write("-"*30 + "\n")
            f.write(email['body'])
            f.write("\n" + "="*50 + "\n\n")

def main():
    try:
        # Example of adding custom stopwords
        custom_stopwords = {'email', 'please', 'regards', 'sincerely', 'dear', 
                          'best', 'thanks', 'thank', 'you', 'gates', 'foundation'}
        
        # Extract emails
        print("Extracting emails...")
        emails = extract_outlook_emails()
        
        if not emails:
            print("No emails were extracted. Please check the folder structure printed above.")
            return
            
        # Analyze keywords
        print("Analyzing keywords...")
        keywords = analyze_keywords(emails, custom_stopwords=custom_stopwords)
        
        # Save results
        print("Saving results...")
        save_results(emails, keywords)
        
        print("Analysis complete! Check 'email_analysis.txt' for results.")
        
        return keywords
        
    except Exception as e:
        print(f"An error occurred: {str(e)}")

if __name__ == "__main__":
    main()