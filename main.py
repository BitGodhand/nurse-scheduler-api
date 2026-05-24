from fastapi import FastAPI
from pydantic import BaseModel
from ortools.sat.python import cp_model

app = FastAPI()

class ScheduleRequest(BaseModel):
    days_in_period: int
    previous_schedule: dict
    requested_days_off: list

@app.post("/generate")
def generate_schedule(req: ScheduleRequest):
    model = cp_model.CpModel()
    
    # Define Staff
    plantilla_nurses = ['N1', 'N2', 'N3', 'N4', 'N5', 'N6', 'N7', 'N8', 'N9']
    jo_nurses = ['N_JO']
    plantilla_na = ['NA1', 'NA2']
    jo_na = ['NA_JO1', 'NA_JO2', 'NA_JO3']
    all_staff = plantilla_nurses + jo_nurses + plantilla_na + jo_na
    shifts = ['AM', 'PM', 'NYT', 'OFF']
    
    work = {}
    # Create grid: Ghost days (-4 to 0) + Period (1 to days_in_period)
    for staff in all_staff:
        for day in range(-4, req.days_in_period + 1):
            for shift in shifts:
                work[(staff, day, shift)] = model.NewBoolVar(f'w_{staff}_{day}_{shift}')
                
    # 1. Lock Ghost Days to Previous Schedule
    for staff in all_staff:
        past_shifts = req.previous_schedule.get(staff, ['OFF', 'OFF', 'OFF', 'OFF', 'OFF'])
        for i, past_shift in enumerate(past_shifts):
            day_idx = i - 4
            for shift in shifts:
                if shift == past_shift:
                    model.Add(work[(staff, day_idx, shift)] == 1)
                else:
                    model.Add(work[(staff, day_idx, shift)] == 0)
                    
    # 2. One shift per day
    for staff in all_staff:
        for day in range(1, req.days_in_period + 1):
            model.AddExactlyOne(work[(staff, day, shift)] for shift in shifts)

    # 3. Fixed Schedules (N1, N2, NA1)
    for day in range(1, req.days_in_period + 1):
        is_weekend = (day % 7 == 6) or (day % 7 == 0) # Assumes Day 1 is a Monday for baseline
        
        if is_weekend:
            model.Add(work[('N1', day, 'OFF')] == 1)
            model.Add(work[('NA1', day, 'OFF')] == 1)
            model.Add(work[('N2', day, 'AM')] == 1)
        else:
            model.Add(work[('N1', day, 'AM')] == 1)
            model.Add(work[('NA1', day, 'AM')] == 1)
            is_monday = (day % 7 == 1)
            is_friday = (day % 7 == 5)
            if is_monday or is_friday:
                model.Add(work[('N2', day, 'OFF')] == 1)
            else:
                model.Add(work[('N2', day, 'PM')] == 1)

    # 4. Shift Coverage
    for day in range(1, req.days_in_period + 1):
        for shift in ['AM', 'PM', 'NYT']:
            p_nurses = sum(work[(n, day, shift)] for n in plantilla_nurses)
            nas = sum(work[(na, day, shift)] for na in (plantilla_na + jo_na))
            model.Add(p_nurses >= 1)
            model.Add(nas >= 1)

    # 5. Consecutive Days Limits
    for staff in all_staff:
        for day in range(-4, req.days_in_period - 4):
            model.Add(sum(work[(staff, d, 'OFF')] for d in range(day, day + 6)) >= 1)
        for day in range(-1, req.days_in_period - 1):
            model.Add(sum(work[(staff, d, 'NYT')] for d in range(day, day + 3)) <= 2)

    # 6. Rest Periods
    for staff in all_staff:
        for day in range(0, req.days_in_period):
            model.AddImplication(work[(staff, day, 'PM')], work[(staff, day+1, 'AM')].Not())
            model.AddImplication(work[(staff, day, 'NYT')], work[(staff, day+1, 'AM')].Not())
            model.AddImplication(work[(staff, day, 'NYT')], work[(staff, day+1, 'PM')].Not())

    # 7. N_JO needs Plantilla Nurse
    for day in range(1, req.days_in_period + 1):
        for shift in ['AM', 'PM', 'NYT']:
            p_nurses = sum(work[(n, day, shift)] for n in plantilla_nurses)
            model.Add(p_nurses >= 1).OnlyEnforceIf(work[('N_JO', day, shift)])

    # 8. Requested Days Off
    for req_off in req.requested_days_off:
        staff_name = req_off.get('staff')
        req_day = req_off.get('day')
        if staff_name in all_staff and 1 <= req_day <= req.days_in_period:
            model.Add(work[(staff_name, req_day, 'OFF')] == 1)

# 9. Rule 3: Job Order Work Days (Proportional)
        # 22 days for a full month (~30 days) is roughly 73% of the time.
        target_jo_days = int((req.days_in_period / 30) * 22)
        
        for jo_staff in jo_nurses + jo_na:
            # Sum up all days where the shift is NOT 'OFF'
            work_days = sum(work[(jo_staff, d, s)] for d in range(1, req.days_in_period + 1) for s in ['AM', 'PM', 'NYT'])
            model.Add(work_days == target_jo_days)
        
        # Solve
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 15.0 
        status = solver.Solve(model)

        if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
            result = {}
            for staff in all_staff:
                result[staff] = []
                for day in range(1, req.days_in_period + 1):
                    for shift in shifts:
                        if solver.Value(work[(staff, day, shift)]) == 1:
                            result[staff].append(shift)
            return {"status": "success", "schedule": result}
        else:
            # DEBUG LOGIC: Check if Requested Off is the problem
            return {
                "status": "failed", 
                "message": "Infeasible: The rules conflict with your staffing levels or requested days off. Check if too many people requested the same day off."
            }
