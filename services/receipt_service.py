import io
def generate_receipt_image(op_type: str, amount_str: str, client_str: str, date_str: str, method_str: str, tx_num: str) -> bytes:
    from services.tasks import generate_receipt_image as _gen
    return _gen(op_type, amount_str, client_str, date_str, method_str, tx_num)
