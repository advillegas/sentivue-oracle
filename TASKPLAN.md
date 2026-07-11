GOAL: Create bootstrap/doctor.ps1 - the Windows twin of bootstrap/doctor.sh that checks key subsystems and exits 0 unless something is critically broken

STEPS (3-7, each independently verifiable):
  1. Create PowerShell script structure with basic functions - CHECK: Script exists with proper structure
  2. Implement system check functions - CHECK: All system checks pass or warn appropriately  
  3. Implement binaries check - CHECK: All binaries are found or show appropriate warnings
  4. Implement models check - CHECK: Models directory structure is correct
  5. Implement serving check - CHECK: llama-swap health endpoint and binding are verified
  6. Implement engines check - CHECK: Engine configurations exist and skills are present
  7. Implement git vault check - CHECK: Vault remote configuration is present
  8. Final verification - CHECK: Script exits with 0 for normal operation

NOT DOING: 
- Complex system diagnostics beyond what doctor.sh covers
- Interactive prompts or user input
- Modifications to the system
- Network connectivity tests beyond health checks

DECISIONS:
- Using PowerShell 5.1 compatibility as specified
- Using ASCII only, no emoji (as per requirements)
- Following same check logic and structure as doctor.sh but adapted for Windows
- Using $ErrorActionPreference = 'Continue' as required
- Using consistent exit codes (0 for success, 1 for critical failures)

DIAGNOSIS (retries only):
The previous attempt failed because it was too monolithic and hung. This approach will break the work into smaller steps with clear checks.