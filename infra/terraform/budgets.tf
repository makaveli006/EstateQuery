# A monthly cost budget that emails when spending is running above what this stack should
# cost. Credits are excluded on purpose: with the account's free credits applied, the net bill
# would stay at zero and a budget on it would only ever warn once the credits had run out.
#
# The stack's normal bill is about $25 a month (one t3.small, its fixed IP and disk), so the
# default limit leaves room above that and the alerts only fire on something unusual, such
# as a resource left running or a spike in data transfer.
#
# Model API spend is not on this bill; it is capped separately on the Anthropic key.

resource "aws_budgets_budget" "monthly" {
  count = var.budget_alert_email == "" ? 0 : 1

  name         = "${var.name}-monthly-cost"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_types {
    include_credit = false
    include_refund = false
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  # Warns before the month ends if the trend says the limit will be passed.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
