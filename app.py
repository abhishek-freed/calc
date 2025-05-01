from flask import Flask, request, jsonify
from flask_cors import CORS
import json
import logging
from typing import Dict, List, Optional, Tuple

app = Flask(__name__)
CORS(app)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants for Indian financial context
DEFAULT_CC_APR = 36.0  # Default credit card APR in %
DEFAULT_PL_APR = 15.0  # Default personal loan APR in %
DEFAULT_LOCKIN = 6  # Minimum lock-in period in months
MAX_REPAYMENT_MONTHS = 120  # 10 years maximum repayment period
MIN_CC_PAYMENT_PERCENT = 0.05  # 5% minimum payment
MIN_CC_PAYMENT_AMOUNT = 100  # ₹100 absolute minimum
ACTIVE_ACCOUNT_STATUS = "11"  # Status code for active accounts

class DebtCalculator:
    """Comprehensive debt calculator with interest savings comparison."""
    
    def __init__(self, json_data: Dict, monthly_savings: float, strategy: str = "avalanche"):
        if monthly_savings <= 0:
            raise ValueError("Monthly savings must be a positive number")
            
        self.json_data = json_data
        self.monthly_savings = monthly_savings
        self.strategy = strategy.lower()
        self._validate_strategy()
        
        self.debt_data = self._process_credit_report()
        self.optimized_plan = None
        self.minimum_payment_plan = None

    def _validate_strategy(self) -> None:
        if self.strategy not in ("avalanche", "snowball"):
            raise ValueError(f"Invalid strategy: {self.strategy}. Choose 'avalanche' or 'snowball'")

    def _process_credit_report(self) -> Dict:
        debt_data = {
            'credit_cards': {},
            'personal_loans': {},
            'apr_rates': {},
            'lock_periods': {}
        }

        try:
            accounts = self.json_data['INProfileResponse']['CAIS_Account']['CAIS_Account_DETAILS']
            for account in accounts:
                if str(account.get('Account_Status')) != ACTIVE_ACCOUNT_STATUS:
                    continue

                self._process_account_type(account, debt_data)
                
        except KeyError as e:
            logger.error(f"Missing required field in credit report: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"Critical error processing credit report: {str(e)}")
            raise
            
        return debt_data

    def _process_account_type(self, account: Dict, debt_data: Dict) -> None:
        acc_type = str(account.get('Account_Type', ''))
        
        if acc_type in {'10', '69'}:  # Credit card types
            self._process_credit_card(account, debt_data)
        elif acc_type == '05' and account.get('Portfolio_Type') == 'I':  # Personal loans
            self._process_personal_loan(account, debt_data)

    def _process_credit_card(self, account: Dict, debt_data: Dict) -> None:
        try:
            acc_number = account.get('Account_Number', 'Unknown_CC')
            balance = safe_float(account.get('Current_Balance'))
            
            if balance <= 0:
                return
                
            apr = safe_float(account.get('Rate_of_Interest'), DEFAULT_CC_APR)
            
            debt_data['credit_cards'][acc_number] = {
                'balance': balance,
                'apr': apr,
                'min_payment': max(balance * MIN_CC_PAYMENT_PERCENT, MIN_CC_PAYMENT_AMOUNT)
            }
            debt_data['apr_rates'][acc_number] = apr
            
        except Exception as e:
            logger.warning(f"Skipping credit card {acc_number}: {str(e)}")

    def _process_personal_loan(self, account: Dict, debt_data: Dict) -> None:
        try:
            acc_number = account.get('Account_Number', 'Unknown_PL')
            balance = safe_float(account.get('Current_Balance'))
            
            if balance <= 0:
                return
                
            sanctioned = safe_float(
                account.get('Highest_Credit_or_Original_Loan_Amount'), 
                balance
            )
            tenure = safe_int(account.get('Repayment_Tenure'), DEFAULT_LOCKIN)
            apr = safe_float(account.get('Rate_of_Interest'), DEFAULT_PL_APR)
            emi = self._calculate_emi(sanctioned, apr, tenure)
            
            debt_data['personal_loans'][acc_number] = {
                'emi': emi,
                'balance': balance,
                'apr': apr,
                'tenure': tenure,
                'original_amount': sanctioned
            }
            debt_data['lock_periods'][acc_number] = tenure
            
        except Exception as e:
            logger.warning(f"Skipping personal loan {acc_number}: {str(e)}")

    def _calculate_emi(self, principal: float, apr: float, tenure: int) -> float:
        if tenure == 0:
            return 0.0
            
        monthly_rate = apr / 12 / 100
        factor = (1 + monthly_rate) ** tenure
        return round((principal * monthly_rate * factor) / (factor - 1), 2)

    def generate_plans(self) -> None:
        self.optimized_plan = self._generate_repayment_plan(optimized=True)
        self.minimum_payment_plan = self._generate_repayment_plan(optimized=False)

    def _generate_repayment_plan(self, optimized: bool) -> Dict:
        cc_balances = {cc: data['balance'] for cc, data in self.debt_data['credit_cards'].items()}
        pl_balances = {pl: data['balance'] for pl, data in self.debt_data['personal_loans'].items()}
        
        plan = {
            'total_interest': 0.0,
            'months': 0,
            'timeline': [],
            'paid_principal': 0.0
        }

        for month in range(1, MAX_REPAYMENT_MONTHS + 1):
            if self._debts_cleared(cc_balances, pl_balances):
                break

            interest = self._calculate_interest(cc_balances, pl_balances)
            plan['total_interest'] += sum(interest['cc'].values()) + sum(interest['pl'].values())

            payments = self._calculate_payments(cc_balances, pl_balances, optimized)
            self._apply_payments(cc_balances, pl_balances, payments)
            
            plan['timeline'].append({
                'month': month,
                'payments': payments,
                'interest': interest,
                'remaining_balances': {
                    'credit_cards': cc_balances.copy(),
                    'personal_loans': pl_balances.copy()
                }
            })
            
            plan['months'] = month

        return plan

    def _calculate_interest(self, cc_balances: Dict, pl_balances: Dict) -> Dict:
        interest = {'cc': {}, 'pl': {}}
        
        for cc, balance in cc_balances.items():
            if balance > 0:
                apr = self.debt_data['credit_cards'][cc]['apr']
                interest['cc'][cc] = balance * (apr / 12 / 100)
        
        for pl, balance in pl_balances.items():
            if balance > 0:
                apr = self.debt_data['personal_loans'][pl]['apr']
                interest['pl'][pl] = balance * (apr / 12 / 100)
        
        return interest

    def _calculate_payments(self, cc_balances: Dict, pl_balances: Dict, optimized: bool) -> Dict:
        payments = {
            'cc': {},
            'pl': {},
            'extra': {'cc': {}, 'pl': {}}
        }
        available = self.monthly_savings

        for cc, data in self.debt_data['credit_cards'].items():
            if cc_balances[cc] > 0:
                payments['cc'][cc] = min(data['min_payment'], cc_balances[cc])
                available -= payments['cc'][cc]

        for pl, data in self.debt_data['personal_loans'].items():
            if pl_balances[pl] > 0:
                payments['pl'][pl] = min(data['emi'], pl_balances[pl])
                available -= payments['pl'][pl]

        if optimized and available > 0:
            debt_priority = self._prioritize_debts(cc_balances, pl_balances)
            for debt in debt_priority:
                debt_type, account, _, _ = debt
                balance = cc_balances[account] if debt_type == 'cc' else pl_balances[account]
                
                if balance <= 0:
                    continue
                    
                payment = min(available, balance)
                
                if debt_type == 'cc':
                    payments['extra']['cc'][account] = payment
                else:
                    payments['extra']['pl'][account] = payment
                    
                available -= payment
                if available <= 0:
                    break

        return payments

    def _prioritize_debts(self, cc_balances: Dict, pl_balances: Dict) -> List[Tuple]:
        debts = []
        
        for cc in cc_balances:
            if cc_balances[cc] > 0:
                debts.append((
                    'cc', cc,
                    self.debt_data['credit_cards'][cc]['apr'],
                    cc_balances[cc]
                ))
        
        for pl in pl_balances:
            if pl_balances[pl] > 0:
                debts.append((
                    'pl', pl,
                    self.debt_data['personal_loans'][pl]['apr'],
                    pl_balances[pl]
                ))

        if self.strategy == "avalanche":
            return sorted(debts, key=lambda x: (-x[2], x[3]))
        else:  # snowball
            return sorted(debts, key=lambda x: (x[3], -x[2]))

    def _apply_payments(self, cc_balances: Dict, pl_balances: Dict, payments: Dict) -> None:
        for cc in cc_balances:
            payment = payments['cc'].get(cc, 0) + payments['extra']['cc'].get(cc, 0)
            cc_balances[cc] = max(0, cc_balances[cc] - payment)

        for pl in pl_balances:
            payment = payments['pl'].get(pl, 0) + payments['extra']['pl'].get(pl, 0)
            pl_balances[pl] = max(0, pl_balances[pl] - payment)

    def _debts_cleared(self, cc_balances: Dict, pl_balances: Dict) -> bool:
        return (all(b <= 0 for b in cc_balances.values()) and 
                all(b <= 0 for b in pl_balances.values()))

    def calculate_interest_savings(self) -> Dict:
        if not self.optimized_plan or not self.minimum_payment_plan:
            self.generate_plans()
        
        return {
            'optimized': {
                'interest': self.optimized_plan['total_interest'],
                'months': self.optimized_plan['months']
            },
            'minimum': {
                'interest': self.minimum_payment_plan['total_interest'],
                'months': self.minimum_payment_plan['months']
            },
            'savings': {
                'interest': self.minimum_payment_plan['total_interest'] - self.optimized_plan['total_interest'],
                'months': self.minimum_payment_plan['months'] - self.optimized_plan['months']
            }
        }

def safe_float(value: Optional[str], default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, '') else default
    except (TypeError, ValueError):
        logger.warning(f"Could not convert {value} to float, using default {default}")
        return default

def safe_int(value: Optional[str], default: int = 0) -> int:
    try:
        return int(value) if value not in (None, '') else default
    except (TypeError, ValueError):
        logger.warning(f"Could not convert {value} to int, using default {default}")
        return default

@app.route('/calculate', methods=['POST'])
def calculate():
    try:
        data = request.json
        report_data = data.get('creditReport')
        monthly_savings = float(data.get('monthlySavings'))
        strategy = data.get('strategy', 'avalanche')
        
        calculator = DebtCalculator(report_data, monthly_savings, strategy)
        calculator.generate_plans()
        results = calculator.calculate_interest_savings()
        
        return jsonify({
            'success': True,
            'results': results,
            'timeline': calculator.optimized_plan['timeline']
        })
    except Exception as e:
        logger.error(f"Error processing request: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 400

if __name__ == '__main__':
    app.run(debug=True) 