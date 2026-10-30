import unittest
from calculator import total

class SumTests(unittest.TestCase):
    def test_empty(self): self.assertEqual(total([]), 0)
    def test_positive(self): self.assertEqual(total([2, 3, 5]), 10)
    def test_negative(self): self.assertEqual(total([-2, 2]), 0)

if __name__ == "__main__": unittest.main()
